"""Offline evidence projection. This module cannot operate or repair CGF."""
from __future__ import annotations

from collections import Counter
from functools import partial
from contextlib import closing
from datetime import datetime
import gzip
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import zlib

from arch4.foundation.runtime_artifacts import read_cgf_evidence_events
from arch4.architecture_contracts.runtime_generation import MatrixGenerationIdentity
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
from arch4.core_contracts.action_reachability import CurrentReachabilityDisposition
from arch4.core_contracts.intelligence_proposals import GRADE_DIMENSIONS
from arch4.command_transactions import CommandTerminalReceipt
from arch4.intelligence_presentation_contract import action_actor_label
from arch4.new_horizon.history import (
    export_history_records, export_history_checkpoints, portable_history_path,
)
from arch4.new_horizon.decision_history import (
    DECISION_HISTORY_KINDS, DecisionLifecycleHistoryOwner, forecast_result_attribution,
)
from arch4.new_horizon.proof import (
    READABLE_REFEREE_PROOF_KINDS, RefereeAssessmentHistoryOwner,
    AQ_HOURLY_CHECKPOINT, AQ_HOURLY_CHECKPOINT_SCHEMA,
)
from arch4.new_horizon.hourly_aq import require_aq_hour
from arch4.privacy_scrub import scrub_record, scrub_text
from tools.log_checker.explanations import (
    FINDINGS, field_explanation, command_explanation, recovery_explanation,
)

VERSION = "3.0"
MAX_JSON = 32 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024
MAX_ROWS = 65536
INPUT_ERRORS = (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
                AttributeError, OverflowError, RecursionError, EOFError, zlib.error)
SECTIONS = (
    "App and shutdown", "Modem readings and timing", "Internet and recovery",
    "Commands", "Learning and forecasts", "Screen consistency", "Record integrity",
)
SNAPSHOTS = (
    "runtime_health", "current_state", "ngui_page_truth",
    "new_horizon_diagnostic_snapshot", "shutdown_receipt",
)
SNAPSHOT_TIMES = (
    ("runtime_health", "ts"), ("current_state", "published_at"),
    ("new_horizon_diagnostic_snapshot", "captured_at"),
    ("ngui_page_truth", "published_at"), ("shutdown_receipt", "finished_at"),
)


class AppRunning(RuntimeError):
    pass


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    try:
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def stamp(value):
    return number(value) or 0.0


def clock(value):
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%d %b %Y %H:%M:%S")
    except (OSError, ValueError, TypeError, OverflowError):
        return "Invalid saved time"


def checked_json_value(value):
    """Bound recursion and reject numbers which cannot be safely reported."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("saved record nesting exceeds safety limit")
        if type(item) in (float, int) and number(item) is None:
            raise ValueError("saved record contains an invalid number")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((v, depth + 1) for v in item)
    return value


def safe(value):
    return scrub_text(str(value))


def context_text(value):
    """Keep physical result identity distinct from an unfulfilled request."""
    d = mapping(value)
    pband = d.get("pband", d.get("serving_band"))
    pool = d.get("ca_pool", ())
    pool = "+".join(f"B{x}" for x in pool if type(x) is int) if isinstance(pool, (list, tuple)) else ""
    parts = [f"B{pband}" if type(pband) is int else "band not recorded",
             pool or "pool not recorded",
             "Cell " + str(d.get("cell_id") if d.get("cell_id") is not None else "—"),
             "PCI " + str(d.get("pci") if d.get("pci") is not None else "—")]
    if d.get("earfcn") is not None:
        parts.append("Channel " + str(d["earfcn"]))
    if d.get("nr_band") is not None:
        parts.append(f"NR n{d['nr_band']} / Cell {d.get('nr_cell_id', '—')} / PCI {d.get('nr_pci', '—')}")
    return " / ".join(parts)


def option_text(value):
    text = str(value or "")
    if text == "do_nothing":
        return "Keep current connection"
    if "PBAND=" in text:
        return text[text.index("PBAND="):].replace("PBAND=", "Band ").replace("|CA=", "; pool ")
    if text.startswith("lte_anchor:"):
        return "Try band " + text.partition(":")[2]
    return safe(text.replace("_", " ")) or "Not recorded"


def blank_report(now):
    return dict(schema="54GUCOfflineReport.v1", checker_version=VERSION,
                generated_at=now, since=now - 86400, until=now, runs=[], findings=[],
                device_mode="unclassified", device_modes={},
                sections={s: [] for s in SECTIONS}, files=[], hourly=[], decisions=[],
                diagnostics=dict(event_counts={}, command_outcomes={}, recovery_state={}),
                field_availability={}, quality_inputs={},
                limitations=[
                    "This report reads saved files and makes no changes to the app, router or History.",
                    "A snapshot describes its saved time. Earlier events use their own retained records.",
                    "Options without saved scores have no reconstructed score. No record does not mean Brain chose to stay.",
                    "Measured quality (AQ) is not proof that a different request would have worked better.",
                    "Times use this PC's local clock. Edge hours may be partial.",
                ])


def finding(report, level, section, text, source, *, code):
    title, meaning, impact, action = FINDINGS[code]
    entry = dict(level=level, section=section, text=safe(text), source=safe(source),
                 code=code, title=title, meaning=meaning, impact=impact, next_step=action)
    if entry not in report["findings"]:
        report["findings"].append(entry)


def note(report, section, text):
    if text not in report["sections"][section]:
        report["sections"][section].append(text)


def read_json(path, report, section, *, required=False):
    if not path.exists():
        if required:
            finding(report, "Missing evidence", section, "This record is missing.", path.name, code='record_missing')
        return {}
    try:
        size = path.stat().st_size
        if size > MAX_JSON:
            raise ValueError("file exceeds the 32 MiB safety limit")
        with path.open("rb") as stream:
            body = stream.read(MAX_JSON + 1)
        if len(body) > MAX_JSON:
            raise ValueError("file grew beyond the safety limit")
        data = checked_json_value(json.loads(body.decode("utf-8-sig")))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        report["files"].append(dict(name=path.parent.name + "/" + path.name, bytes=size,
                                     sha256=hashlib.sha256(body).hexdigest(), status="Read"))
        return data
    except INPUT_ERRORS as exc:
        finding(report, "Missing evidence", section, "Cannot read saved record: " + type(exc).__name__, path.name, code='record_unreadable')
        return {}


def snapshot_database(source, destination):
    """Read-only SQLite backup includes a retained WAL; no raw file copying."""
    if not source.is_file():
        raise FileNotFoundError(source.name)
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 60:
            raise TimeoutError("database snapshot exceeded 60 seconds")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
        src.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=1024, progress=progress, sleep=0.05)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("database integrity check failed")


def recorded_device_mode(core, *, support=False):
    """Project the saved capability owner; transient service cannot select a class."""
    if support:
        from arch4.support_evidence import read_support_facts
        identity, facts = read_support_facts(core)
        exact = facts.__getitem__
    else:
        facts = CoreAcceptedCurrentFacts.from_payload(core["accepted_current_facts"])
        identity, exact = facts.matrix_identity, facts.exact
        if identity != MatrixGenerationIdentity.from_mapping(core["matrix_identity"]):
            raise ValueError("checker_device_class_generation_mismatch")
    capability = exact("capability.device_radio_class")
    status = capability.value.require("value") if capability.disposition == "available" else None
    mode = "5G" if status == "5g" else "4G" if status == "4g_only" else "unclassified"
    result = dict(mode=mode, device_radio_class=status,
                  source="CoreAcceptedCurrentFacts:capability.device_radio_class",
                  matrix_identity=identity.as_dict(), readings={})
    paths = ["radio.network_type", "radio.nr5g_active", "rf.pci", "rf.sinr_db",
             "rf.rsrp_dbm", "rf.rsrq_db", "rf.rssi_dbm"]
    if mode == "5G":
        paths.extend(("radio.nr_band", "radio.nr_pci", "radio.nr_arfcn", "radio.nr_cell_id",
                      "radio.nr_sinr_db", "radio.nr_rsrp_dbm", "radio.nr_rsrq_db"))
    for path in paths:
        try:
            fact = exact(path)
        except KeyError:
            result["readings"][path] = dict(state="not_recorded", value=None)
            continue
        result["readings"][path] = dict(state=fact.disposition,
            value=fact.value.require("value"), retained=fact.retention is not None,
            sampled_at=fact.sampled_at, acquired_at=fact.acquired_at, reason=fact.reason)
    active = result["readings"]["radio.nr5g_active"]
    result["nr_active"] = active["value"] if active["state"] == "available" and type(active["value"]) is bool else None
    return result


def select_report_mode(report):
    classes = {row["mode"] for row in report["device_modes"].values()
               if row["mode"] in {"4G", "5G"}}
    if len(classes) > 1:
        report["device_mode"] = "conflicting"
        finding(report, "Problem", SECTIONS[6],
                "Conflicting device classes occur in the same retained router context. Learning records were not combined.", "CoreAcceptedCurrentFacts", code='device_class_conflict')
    else:
        report["device_mode"] = next(iter(classes), "unclassified")
    mode = report["device_mode"]
    if mode == "5G":
        note(report, SECTIONS[4], "5G device review: the 5g-combined-v1 model uses a -3 to +3 service grade. Older records retain their recorded grading scale. LTE-only service does not turn a 5G device into a 4G device. LTE and NR readings are reported separately; grades are not recalculated.")
    elif mode == "4G":
        note(report, SECTIONS[4], "4G device review: the existing service grade and historical results retain their original scale. No NR bonus is added by this checker.")


def inspect_field_availability(report, name, core, *, support=False):
    """List the accepted owner's entire denominator, without copying private values."""
    try:
        if support:
            from arch4.support_evidence import read_support_facts
            _, facts = read_support_facts(core)
            facts = facts.values()
        else:
            accepted = CoreAcceptedCurrentFacts.from_payload(core['accepted_current_facts'])
            if accepted.matrix_identity != MatrixGenerationIdentity.from_mapping(core['matrix_identity']):
                raise ValueError('checker_fields_generation_mismatch')
            facts = accepted.facts
        rows = [dict(field=f.path, state=f.disposition, reason=f.reason,
                     retained=f.retention is not None, sampled_at=f.sampled_at,
                     acquired_at=f.acquired_at) for f in facts]
    except (KeyError, ValueError, TypeError, AttributeError):
        finding(report, 'Missing evidence', SECTIONS[1],
                'The complete accepted-field inventory is absent or invalid; no zero-missing result was invented.',
                name, code='accepted_fields_unreadable')
        return
    counts = dict(Counter(row['state'] for row in rows))
    report['field_availability'][name] = dict(counts=counts, fields=rows)
    note(report, SECTIONS[1], name + ': saved accepted-field availability: ' +
         ', '.join(f'{key} {value}' for key, value in sorted(counts.items())) +
         '. Availability states are not bug counts. Cell absence and unsupported fields remain distinct in the recorded reasons.')
    missing = sum(counts.get(state, 0) for state in ('missing', 'invalid'))
    if missing:
        finding(report, 'Missing evidence', SECTIONS[1],
                f'{missing} accepted fields were missing or invalid in this saved snapshot. See Saved input availability for their exact names and reasons.',
                name, code='field_values_missing')


def inspect_quality_inputs(report, observations):
    """Inventory recorded receipts only; never recalculate BQ, CQ, CQB or AQ."""
    models, missing = Counter(), Counter()
    available = lte_used = 0
    for row in observations:
        payload = mapping(row.get('payload'))
        model = payload.get('quality_model')
        models[model if isinstance(model, str) else 'not recorded'] += 1
        receipt = mapping(payload.get('quality_radio_inputs'))
        available += bool(receipt)
        used = mapping(receipt.get('used'))
        lte_used += bool(mapping(used.get('bq')) and mapping(used.get('cqb')))
        required = payload.get('missing_required')
        if isinstance(required, list):
            missing.update(path for path in required if isinstance(path, str))
    absent = len(observations) - available
    report['quality_inputs'] = dict(observations=len(observations), models=dict(models),
        receipt_available=available, receipt_not_retained=absent,
        lte_used_receipts=lte_used, missing_required=dict(missing))
    if observations:
        note(report, SECTIONS[1], f'Quality input receipts: {available} retained; {absent} not retained; '
             f'{lte_used} include the explicit LTE BQ/CQB used inputs. Older records are not filled with invented inputs.')
    if missing:
        note(report, SECTIONS[1], 'Recorded missing required inputs: ' +
             ', '.join(f'{key} ({count} observations)' for key, count in sorted(missing.items())) + '.')
    if absent:
        finding(report, 'Missing evidence', SECTIONS[1],
                f'{absent} observations have no retained quality-input receipt. This does not establish that the app lacked those values at the time.',
                'History/observation', code='quality_receipts_missing')


def inspect_snapshots(report, name, saved, *, support=False):
    health = saved["runtime_health"]
    diag = saved["new_horizon_diagnostic_snapshot"]
    state = saved["current_state"]
    page = saved["ngui_page_truth"]
    faults = mapping(health.get("faults"))
    if stamp(faults.get("fault_count")) > 0:
        finding(report, "Problem", SECTIONS[0], f"The app recorded {faults['fault_count']} runtime faults. A later recovery does not erase them.", name, code='runtime_faults')
    workers = mapping(health.get("workers"))
    normal_shutdown = (health.get("status_phase") == "shutdown"
                       and (saved["shutdown_receipt"].get("status") == "complete"
                            or health.get("phase") == "shutdown_complete")
                       and not workers.get("required_failed")
                       and not workers.get("optional_failed")
                       and workers.get("degraded") is False)
    if health.get("degraded") or (workers.get("failed") and not normal_shutdown):
        finding(report, "Warning", SECTIONS[0], "The last runtime-health record reports a degraded or failed worker.", name, code='worker_degraded')
    if normal_shutdown:
        note(report, SECTIONS[0], f"{name}: stopped workers in the completed shutdown are not treated as runtime failures.")
    shut = saved["shutdown_receipt"]
    if shut:
        note(report, SECTIONS[0], f"{name}: shutdown was reported as {shut.get('status', 'not recorded')}.")
        if shut.get("status") != "complete":
            finding(report, "Warning", SECTIONS[0], "Shutdown did not report a clean completion.", name, code='shutdown_unclean')
    else:
        finding(report, "Missing evidence", SECTIONS[0], "No clean-shutdown receipt was retained. This alone does not prove a crash.", name, code='shutdown_missing')
    core_health = mapping(state.get("core_current_health"))
    fast = mapping(core_health.get("modem_live_fast_lane"))
    if fast:
        note(report, SECTIONS[1], f"{name}: {fast.get('miss_count', 'unrecorded')} missed reads reported; final reading stale: {fast.get('stale', 'not recorded')}.")
        if fast.get("publication_sink_error"):
            finding(report, "Problem", SECTIONS[1], "The modem reading publication reported an error.", name, code='modem_publication_error')
    else:
        finding(report, "Missing evidence", SECTIONS[1], "No modem-read timing snapshot was retained.", name, code='modem_timing_missing')
    note(report, SECTIONS[1], "A saved sample gap does not prove a modem access-rule violation; every physical request timestamp is not retained here.")
    pop = mapping(diag.get("learning_population"))
    if pop:
        if pop.get("coherent") is False or pop.get("learning_population_id") != pop.get("forecast_population_id"):
            finding(report, "Problem", SECTIONS[4], "Learning and forecasting do not use the same saved assessment population.", name, code='learning_population_mismatch')
        note(report, SECTIONS[4], f"{name}: {pop.get('source_assessment_count', 'unrecorded')} source assessments; {pop.get('learned_assessment_count', 'unrecorded')} learned assessments. These counts need not be equal.")
    else:
        finding(report, "Missing evidence", SECTIONS[4], "No learning snapshot was retained. Brain may have been off.", name, code='learning_snapshot_missing')
    log = mapping(mapping(diag.get("history")).get("log_processor"))
    for key in ("flush_failures", "critical_loss_count", "routine_loss_count"):
        if stamp(log.get(key)) > 0:
            finding(report, "Problem", SECTIONS[6], f"History reports {key.replace('_', ' ')}: {log[key]}.", name, code='history_loss')
    for key in ("decision_lifecycle_history", "referee_assessment_history"):
        view = mapping(diag.get(key))
        if stamp(view.get("rejected_count")):
            finding(report, "Warning", SECTIONS[4], f"{view['rejected_count']} retained {key.replace('_', ' ')} records were rejected by their owner.", name, code='history_owner_rejections')
    core = mapping(state.get("core_snapshot"))
    inspect_field_availability(report, name, core, support=support)
    try:
        device = recorded_device_mode(core, support=support)
    except (KeyError, ValueError, TypeError, AttributeError):
        device = dict(mode="unclassified", readings={})
    report["device_modes"][name] = device
    if device["mode"] == "unclassified":
        finding(report, "Missing evidence", SECTIONS[6],
                "The recorded device class is missing, unqualified or invalid. 4G/5G mode was not guessed from the current radio link or model name.", name, code='device_class_missing')
    else:
        note(report, SECTIONS[1], f"{name}: {device['mode']} device; current NR link active: {device['nr_active'] if device['nr_active'] is not None else 'not recorded'}.")
        if device["mode"] == "5G":
            for rat, prefix in (("LTE", "rf."), ("NR", "radio.nr_")):
                states = Counter(row["state"] for path, row in device["readings"].items() if path.startswith(prefix))
                note(report, SECTIONS[1], f"{name}: {rat} readings: " + ", ".join(f"{key} {count}" for key, count in sorted(states.items())) + ". Missing measurements remain missing.")
    core_id = mapping(core.get("matrix_identity"))
    delivery = mapping(page.get("chart_delivery"))
    fields = mapping(page.get("fields"))
    reg = mapping(mapping(core.get("registers")).get("internet_reachability"))
    reach = mapping(reg.get("value"))
    page_net = mapping(fields.get("quality.internet_reachable")).get("value")
    # Only equal-generation snapshots can establish a cross-owner contradiction.
    same = (core_id.get("matrix_sequence") is not None
            and core_id.get("matrix_sequence") == delivery.get("front_source_sequence")
            and core_id.get("source_epoch") == delivery.get("front_source_epoch"))
    core_net = {
        CurrentReachabilityDisposition.REACHABLE.value: True,
        CurrentReachabilityDisposition.OUTAGE.value: False,
    }.get(reach.get("disposition"))
    if same and type(core_net) is bool and type(page_net) is bool:
        if core_net != page_net:
            finding(report, "Problem", SECTIONS[5], "The saved screen and Core disagree about Internet state for the same sample.", name, code='screen_net_mismatch')
        else:
            note(report, SECTIONS[5], f"{name}: Core and the saved screen agree about Internet state.")
    else:
        note(report, SECTIONS[5], f"{name}: exact same-sample screen/NET comparison is unavailable; asynchronous snapshots are not treated as a defect.")
    note(report, SECTIONS[5], "The saved screen record is not a screenshot. This checker cannot prove visual layout or line rendering.")


def inspect_events(report, name, events):
    downs, recovery, commands = [], [], []
    field_read_warnings = field_read_recoveries = 0
    repeated = Counter()
    for event in events:
        kind = str(event.get("kind", ""))
        repeated[(kind, event.get("transaction_id", ""), event.get("ts"))] += 1
        if event.get("severity") in ("ERROR", "CRITICAL"):
            finding(report, "Warning", SECTIONS[0], "The diagnostic journal records an error: " + kind.replace("_", " "), name, code='diagnostic_error')
        if kind == "core_internet_timeline_change":
            cur = mapping(mapping(event.get("data")).get("current"))
            if cur.get("disposition") == CurrentReachabilityDisposition.OUTAGE.value:
                downs.append(event)
            outage = mapping(cur.get("latest_completed_outage"))
            if outage:
                seconds = number(outage.get("duration_s", outage.get("outage_s")))
                if seconds is not None:
                    start = stamp(outage.get("started_at"))
                    note(report, SECTIONS[2], f"{name}: Core recorded an Internet interruption at {clock(start) if start else 'an unrecorded time'}, lasting {seconds:.1f} seconds.")
        if kind == "profile_field_conversion":
            field_read_warnings += 1
        elif kind == "profile_field_recovery":
            field_read_recoveries += 1
        elif "emergency" in kind or "recovery" in kind:
            recovery.append(event)
        if "command" in kind:
            commands.append(event)
    report["diagnostics"]["event_counts"][name] = dict(
        net_down=len(downs), recovery=len(recovery), commands=len(commands),
        field_read_warnings=field_read_warnings, field_read_recoveries=field_read_recoveries)
    if field_read_warnings or field_read_recoveries:
        note(report, SECTIONS[1], f"{name}: {field_read_warnings} reading warnings; {field_read_recoveries} reading warnings cleared.")
    if downs:
        note(report, SECTIONS[2], f"{name}: {len(downs)} Internet disconnect records.")
    if recovery:
        note(report, SECTIONS[2], f"{name}: {len(recovery)} recovery records.")
    if commands:
        note(report, SECTIONS[3], f"{name}: {len(commands)} command records.")
    duplicates = sum(c-1 for c in repeated.values() if c > 1)
    if duplicates:
        finding(report, "Warning", SECTIONS[6], f"{duplicates} records repeat the same event type, time and transaction. Check whether these are separate stages.", name, code='duplicate_events')


def inspect_intelligence_faults(path, report):
    """Read the independent owner's retained causes, never worker health proxies."""
    from tools.log_checker.bundle_input import BundleHistory
    checkpoints = path.checkpoints if isinstance(path, BundleHistory) else partial(export_history_checkpoints, path)
    for checkpoint in checkpoints(name="runtime_faults_v2"):
        if checkpoint["scope_key"] != "new_horizon_runtime":
            continue
        payload = checkpoint["payload"]
        if payload["schema"] != "NewHorizonRuntimeFaults.v2":
            raise ValueError("intelligence_fault_checkpoint_schema_invalid")
        for cause in payload["faults"].values():
            first, last = number(cause["first_at"]), number(cause["last_at"])
            count = cause["count"]
            if first is None or last is None or first > last or type(count) is not int or count < 1:
                raise ValueError("intelligence_fault_checkpoint_cause_invalid")
            if last < report["since"] or first > report["until"]:
                continue
            endpoint_in_window = (report["since"] <= first <= report["until"]
                                  or report["since"] <= last <= report["until"])
            exact = first >= report["since"] and last <= report["until"]
            extent = f"{count} occurrences in this window" if exact else (
                f"{count} lifetime occurrences; exact window count unavailable")
            text = ("Intelligence runtime: " + safe(cause["operation"]) + ": "
                    + safe(cause["error_type"]) + ": " + safe(cause["detail"])
                    + f" ({extent}; first {clock(first)}, last {clock(last)}).")
            if not endpoint_in_window:
                text += " Aggregate spans the window; occurrence inside it is not established."
            finding(report, "Problem" if endpoint_in_window else "Missing evidence",
                    SECTIONS[4], text, "History/runtime_faults_v2", code='intelligence_fault')
        if payload["evicted_causes"] and checkpoint["updated_at"] >= report["since"]:
            finding(report, "Missing evidence", SECTIONS[6],
                    "Intelligence fault history has evicted causes; retained causes are not a complete fault inventory.",
                    "History/runtime_faults_v2", code='fault_causes_evicted')


def read_history(path, continuity, report):
    """Payload decoding/validation stays in the existing owners."""
    from tools.log_checker.bundle_input import BundleHistory, EVALUATION_SUMMARY_SCHEMA
    read_records = path.records if isinstance(path, BundleHistory) else partial(export_history_records, path)
    read_checkpoints = path.checkpoints if isinstance(path, BundleHistory) else partial(export_history_checkpoints, path)
    try:
        inspect_intelligence_faults(path, report)
    except INPUT_ERRORS as exc:
        finding(report, "Missing evidence", SECTIONS[4], "Intelligence fault history could not be fully read: " + type(exc).__name__, "History/runtime_faults_v2", code='fault_history_unreadable')
    records, hours = [], []
    evaluations = []
    try:
        decoded = read_records(kind="brain_evaluation_v1", scope_key=continuity,
                                        since=report["since"], limit=MAX_ROWS)
        for row in decoded:
            try:
                checked_json_value(row)
                p = mapping(row["payload"])
                expected_schema = (EVALUATION_SUMMARY_SCHEMA if isinstance(path, BundleHistory)
                                   else "NewHorizonBrainEvaluation.v1")
                if (p.get("schema") != expected_schema
                        or not isinstance(p.get("reason"), str)
                        or not isinstance(p.get("selected_opportunity_id"), (str, type(None)))):
                    raise ValueError("brain_evaluation_schema_invalid")
                if row["observed_at"] <= report["until"]:
                    evaluations.append(row)
            except INPUT_ERRORS as exc:
                finding(report, "Missing evidence", SECTIONS[4], "A saved Brain evaluation is malformed: " + type(exc).__name__, "History/brain_evaluation_v1", code='evaluation_malformed')
    except INPUT_ERRORS as exc:
        finding(report, "Missing evidence", SECTIONS[4], "Brain evaluations could not be fully read: " + type(exc).__name__, "History/brain_evaluation_v1", code='evaluations_unreadable')
    report["brain_evaluations"] = evaluations
    note(report, SECTIONS[4], f"Retained Brain evaluations: {len(evaluations)} (includes no-action decisions).")
    if len(evaluations) == MAX_ROWS:
        finding(report, "Missing evidence", SECTIONS[4], "Brain evaluation export reached the report limit.", "History", code='evaluation_limit')
    if not evaluations:
        note(report, SECTIONS[4], "No historical Brain evaluations retained; action proposals alone cannot explain no-action decisions.")
    if isinstance(path, BundleHistory):
        catalog = path.catalog(report["since"]-720)
    else:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            catalog = con.execute("SELECT kind,scope_key,COUNT(*) FROM evidence WHERE observed_at>=? GROUP BY kind,scope_key", (report["since"]-720,)).fetchall()
    supported = set(DECISION_HISTORY_KINDS) | set(READABLE_REFEREE_PROOF_KINDS) | {"referee_partial_context_v1", "referee_timeline_discontinuity_v1"}
    for kind, scope, count in catalog:
        if scope != continuity or kind not in supported:
            continue
        if count > MAX_ROWS:
            finding(report, "Missing evidence", SECTIONS[6], f"{kind}: only the latest {MAX_ROWS} records fit the report limit.", "History", code='history_family_limit')
        try:
            records.extend(read_records(kind=kind, scope_key=continuity,
                                                 since=report["since"]-720, limit=MAX_ROWS))
        except INPUT_ERRORS as exc:
            finding(report, "Missing evidence", SECTIONS[6], "A History record family could not be decoded: " + type(exc).__name__, kind, code='history_family_unreadable')
    records = [r for r in records if r["observed_at"] <= report["until"]]
    decisions = DecisionLifecycleHistoryOwner.snapshot_from_records(
        continuity, [r for r in records if r["kind"] in DECISION_HISTORY_KINDS])
    proofs = RefereeAssessmentHistoryOwner.snapshot_from_records(
        continuity, [r for r in records if r["kind"] in READABLE_REFEREE_PROOF_KINDS])
    for label, rejected in (("Decision", decisions.rejected), ("Referee", proofs.rejected)):
        if rejected:
            finding(report, "Warning", SECTIONS[4], f"{len(rejected)} {label} records failed canonical validation and were excluded.", "History", code='canonical_rejections')
    if decisions.unjoined:
        finding(report, "Missing evidence", SECTIONS[3], f"{len(decisions.unjoined)} saved command results have no retained parent decision in the review window.", "History", code='command_parent_missing')
    valid_proofs = list(proofs.records)
    scopes = {p["payload"].get(k) for p in valid_proofs for k in ("baseline_scope_key", "result_scope_key")}
    for cp in read_checkpoints(name=AQ_HOURLY_CHECKPOINT):
        if cp["scope_key"] != continuity:
            continue
        if cp["payload"].get("schema") != AQ_HOURLY_CHECKPOINT_SCHEMA:
            raise ValueError("hourly history schema is not supported")
        for raw in cp["payload"].get("hours", []):
            hour = require_aq_hour(raw)
            if hour["continuity_key"] != continuity:
                raise ValueError("hourly history identity mismatch")
            if report["since"] <= hour["last_at"] <= report["until"]:
                hours.append(hour)
                scopes.add(hour["identity"].get("scope_key"))
    # Completed learning proof is evidence of identity, not an observation inventory.
    qualified_scopes = {s for s in scopes if isinstance(s, str) and s}
    stored_scopes = {scope for kind, scope, _ in catalog if kind in ("observation", "observation_batch")}
    observations, unqualified, other_context = [], 0, 0
    for scope in sorted(qualified_scopes | stored_scopes):
        try:
            decoded = read_records(kind="observation", scope_key=scope,
                                             since=report["since"], limit=MAX_ROWS)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            finding(report, "Missing evidence", SECTIONS[6],
                    "A retained observation scope could not be decoded: " + type(exc).__name__, "History", code='observation_scope_unreadable')
            continue
        if len(decoded) == MAX_ROWS:
            finding(report, "Missing evidence", SECTIONS[6],
                    "The observation export limit was reached; earlier observations may be missing.", "History", code='observation_limit')
        for row in decoded:
            if not report["since"] <= row["observed_at"] <= report["until"]:
                continue
            # New observations carry their canonical population even before
            # the first mature proof. Older evidence needs an exact scope link.
            owner_scope = row["payload"].get("continuity_key")
            if owner_scope == continuity or (owner_scope is None and scope in qualified_scopes):
                observations.append(row)
            elif isinstance(owner_scope, str) and owner_scope.strip():
                other_context += 1
            else:
                unqualified += 1
    observations.sort(key=lambda row: (row["observed_at"], row["event_key"]))
    inspect_quality_inputs(report, observations)
    if not observations and not any(scope == continuity for _, scope, _ in catalog):
        finding(report, "Missing evidence", SECTIONS[4],
                "No recent History records match this router's recorded SIM/network continuity identity.", "History", code='history_context_missing')
    if unqualified:
        finding(report, "Missing evidence", SECTIONS[6],
                f"{unqualified} observations lack a matching retained SIM/network context and were excluded.", "History", code='observation_identity_missing')
    if other_context:
        note(report, SECTIONS[6],
             f"{other_context} observations belong to other recorded SIM/network contexts and were excluded.")
    note(report, SECTIONS[6], "Observation coverage uses canonical historical contexts and confirmed retained-run identities. Observation rows do not retain source epochs; exact-epoch coverage is not claimed.")
    note(report, SECTIONS[6], f"History: {len(observations)} decoded same-context observations, {len(valid_proofs)} canonical proof records, {len(decisions.attributable_decisions)} saved Brain proposals and {len(hours)} hourly quality records.")
    if not observations:
        finding(report, "Missing evidence", SECTIONS[1], "No observations could be joined to the retained canonical router contexts.", "History", code='observations_missing')
    else:
        obs = [r["payload"] for r in observations]
        note(report, SECTIONS[1], f"{sum(p.get('complete') is False for p in obs)} observations explicitly contain missing or retained fields; these are not counted as complete fresh measurements.")
    unhandled = sorted({kind for kind, scope, count in catalog if scope == continuity
                        and kind not in supported and kind != "brain_evaluation_v1"})
    if unhandled:
        note(report, SECTIONS[6], "Additional History record families are inventoried but not interpreted: " + ", ".join(unhandled) + ".")
    extras = [r for r in records if r["kind"] not in DECISION_HISTORY_KINDS and r["kind"] not in READABLE_REFEREE_PROOF_KINDS]
    return list(decisions.retained_records) + extras, valid_proofs, hours, observations


def proof_grade_text(payload):
    """Format a Referee dimension without turning a rules score into AQ."""
    dimension = payload.get("dimension")
    if dimension not in GRADE_DIMENSIONS:
        raise ValueError("checker_proof_dimension_invalid")
    label = "AQ" if dimension == "service_outcome" else "Rules/safety"
    grade = number(payload.get("grade"))
    status = str(payload.get("status", "not graded"))
    if status == "graded" and payload.get("final_assessment") is True and grade is not None:
        return f"; {label} {grade:+.1f}"
    return f"; {label}: {status}" + ("; not final" if payload.get("final_assessment") is not True else "")


def proof_origin_text(payload):
    """Continuous observations have no action actor; actions use one vocabulary."""
    if payload.get("subject_kind") == "active_context_window":
        if payload.get("origin") != "continuous_context":
            raise ValueError("checker_context_origin_invalid")
        return "Continuous monitoring"
    return action_actor_label(payload.get("origin"), allow_external=True)


def make_decisions(records, proofs, report, *, command_terminals=(), session_scope=""):
    command_terminals = [r.as_dict() if isinstance(r, CommandTerminalReceipt) else r for r in command_terminals]
    terminals = {r["payload"].get("proposal_id"): r["payload"] for r in records if r["kind"] == "decision_terminal_v2"}
    results = {r["payload"].get("proposal_id"): r["payload"] for r in records if r["kind"] == "proposal_result"}
    seen = set()
    for row in sorted(records, key=lambda r: r["observed_at"]):
        if not row["kind"].startswith("decision_contribution_"):
            continue
        p = row["payload"]
        pid = p["proposal_id"]
        if pid in seen:
            continue
        seen.add(pid)
        if not report["since"] <= row["observed_at"] <= report["until"]:
            continue
        frame = mapping(p.get("decision_frame"))
        forecast = mapping(p.get("forecast"))
        selected = forecast.get("opportunity_id")
        # A prediction is not proof of an executed or even submitted command.
        selected_text = option_text(selected) if selected else "Selected target not retained"
        prediction = number(forecast.get("expected_aq"))
        if prediction is not None:
            selected_text += f"; expected AQ {prediction:+.1f}"
        if forecast.get("evidence_cell_id") is not None:
            selected_text += "; forecast Cell " + str(forecast["evidence_cell_id"])
        if forecast.get("evidence_pci") is not None:
            selected_text += "; PCI " + str(forecast["evidence_pci"])
        actual = []
        final_command_proof = False
        for proof in proofs:
            v = proof["payload"]
            if v.get("decision_id") not in (pid, "core-command:" + pid):
                continue
            if (v.get("subject_kind") == "core_command"
                    and v.get("dimension") == "service_outcome"
                    and v.get("final_assessment") is True
                    and v.get("status") == "graded"):
                final_command_proof = True
            text = context_text(v.get("result_identity"))
            text += proof_grade_text(v)
            att = forecast_result_attribution(forecast, v.get("result_identity"))
            if att["reason"] == "different_serving_cell":
                text += "; different cell: not a failed Brain prediction"
            actual.append(text)
        result = results.get(pid, {})
        terminal = terminals.get(pid, {})
        command = next((r for r in command_terminals
            if r.get("command_id") == pid and session_scope
            and mapping(r.get("matrix_identity")).get("session_scope_key") == session_scope
            and mapping(r.get("matrix_identity")).get("matrix_sequence") == frame.get("matrix_sequence")
            and row["observed_at"] <= r["terminal_ts"] <= report["until"]), None)
        status = ("Submitted" if result.get("submitted") is True else
                  "Not submitted" if result.get("submitted") is False else "Submission not recorded")
        if terminal:
            status += "; " + safe(terminal.get("reason", "terminal recorded").replace("_", " "))
        elif command is not None and result.get("submitted") is not False:
            status += "; Command Authority: " + command["terminal_status"] + "; " + safe(command["reason"].replace("_", " "))
        elif (not final_command_proof and result.get("submitted") is not False
              and report["until"] - row["observed_at"] > 720):
            finding(report, "Missing evidence", SECTIONS[3], "A Brain proposal has no retained terminal or final command proof in this review. This is not proof that a modem command is still running.", clock(row["observed_at"]), code='proposal_finish_missing')
        candidates = frame.get("candidate_ids", [])
        if not isinstance(candidates, (list, tuple)) or any(not isinstance(i, str) for i in candidates):
            finding(report, "Missing evidence", SECTIONS[4], "Saved decision options are malformed; other decisions remain readable.", clock(row["observed_at"]), code='decision_options_malformed')
            candidates = []
        options = [option_text(i) for i in candidates]
        report["decisions"].append(dict(at=row["observed_at"], options=options,
            selected=selected_text, reason=safe(p.get("explanation", "Reason not retained")),
            result=" | ".join(dict.fromkeys(actual)) or status,
            scores="Only the selected forecast is retained; other option scores are not recorded."))


def hourly_rows(report, proofs, hours, diagnostics):
    first = datetime.fromtimestamp(report["since"]).astimezone().replace(minute=0, second=0, microsecond=0).timestamp()
    current = first
    while current < report["until"]:
        lo, hi = max(current, report["since"]), min(current + 3600, report["until"])
        actual, options, selected, results = [], [], [], []
        for hour in hours:
            if hour["first_at"] < hi and hour["last_at"] > lo:
                actual.append(context_text(hour["identity"]) + f"; hourly AQ {hour['aq']:+.1f}; {hour['coverage_s']/60:.0f} min saved in that hour")
        for row in proofs:
            p = row["payload"]
            if stamp(p.get("opened_at")) < hi and stamp(p.get("closed_at")) > lo:
                actual.append(context_text(p.get("result_identity")) + proof_grade_text(p) + "; " + proof_origin_text(p))
        for d in report["decisions"]:
            if lo <= d["at"] < hi:
                when = datetime.fromtimestamp(d["at"]).astimezone().strftime("%H:%M:%S")
                options.append(when + ": " + (" | ".join(d["options"]) or "Options not recorded"))
                selected.append(when + ": " + d["selected"] + ". " + d["reason"])
                results.append(when + ": " + d["result"])
        for diag in diagnostics:
            at = stamp(diag.get("captured_at"))
            chain = mapping(diag.get("adviser_chain"))
            if lo <= at < hi and chain.get("status") == "complete":
                when = datetime.fromtimestamp(at).astimezone().strftime("%H:%M:%S")
                saved_options = []
                candidates = chain.get("candidates")
                if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
                    finding(report, "Missing evidence", SECTIONS[4], "Saved adviser options are malformed; they were not guessed.", clock(at), code='adviser_options_malformed')
                    continue
                for c in candidates:
                    cb = mapping(c.get("crystal_ball"))
                    aq = number(cb.get("expected_aq"))
                    detail = option_text(c.get("opportunity_id"))
                    if aq is not None:
                        detail += f"; forecast AQ {aq:+.1f}"
                    elif (value := number(cb.get("expected_service_value"))) is not None:
                        detail += f"; forecast service value {value:.3f}"
                    saved_options.append(detail)
                options.append(when + " saved snapshot: " + " | ".join(saved_options))
                selected.append(when + " saved snapshot: " + option_text(chain.get("selected_opportunity_id")) + "; " + safe(str(chain.get("selection_reason", "")).replace("_", " ")))
        evaluations = [r for r in report.get("brain_evaluations", []) if lo <= r["observed_at"] < hi]
        groups = Counter((option_text(r["payload"].get("selected_opportunity_id")),
                          safe(r["payload"]["reason"].replace("_", " "))) for r in evaluations)
        for (choice, reason), count in sorted(groups.items()):
            selected.append(f"{count} evaluations: {choice}; {reason}.")
        report["hourly"].append(dict(hour=datetime.fromtimestamp(current).strftime("%d %b %Y %H:00"), start=lo, end=hi,
            interval=clock(lo) + " to " + clock(hi),
            actual=list(dict.fromkeys(actual)), options=list(dict.fromkeys(options)),
            selected=list(dict.fromkeys(selected)), results=list(dict.fromkeys(results))))
        current += 3600


def analyse(app_root, *, now=None, history_path=None, running=lambda: False):
    """All reads are stopped-store diagnostics. A restart aborts publication."""
    if running():
        raise AppRunning("54GUC is running. Waiting for it to close.")
    root = Path(app_root).resolve()
    now = time.time() if now is None else float(now)
    report = blank_report(now)
    runtime = root / "portable_runtime"
    run_dirs = sorted((p for p in runtime.glob("guc_output*") if p.is_dir() and not p.is_symlink()), reverse=True)
    saved_runs = []
    read_run_count = 0
    for run in run_dirs:
        health_path = run / "runtime_health.json"
        if not health_path.exists() and not (run / "current_state.json").exists():
            continue
        if read_run_count >= 128:
            finding(report, "Missing evidence", SECTIONS[6], "More than 128 runs exist; remaining runs were not read.", "portable_runtime", code='run_limit')
            break
        read_run_count += 1
        saved = {n: read_json(run / (n + ".json"), report, SECTIONS[0], required=n in ("runtime_health", "current_state")) for n in SNAPSHOTS}
        saved_times = [value for name, field in SNAPSHOT_TIMES
                       if (value := number(saved[name].get(field))) is not None and value > 0]
        if any(value > now for value in saved_times):
            finding(report, "Missing evidence", SECTIONS[6],
                    "A run contains a saved app time later than this review and was excluded; its time and connection were not used to select the review window.",
                    run.name, code='run_time_invalid')
            continue
        saved_runs.append((run, saved, max(saved_times, default=0.0)))
    if not saved_runs:
        finding(report, "Missing evidence", SECTIONS[0], "No usable retained run was found in this app folder.", "portable_runtime", code='runs_missing')
    # Most recently sampled run is the identity anchor, not whichever folder sorted last.
    # Saved History may have advanced in another installation; it cannot move
    # this folder's app-record window or supply a different identity anchor.
    saved_runs.sort(key=lambda item: item[2])
    last_saved_at = saved_runs[-1][2] if saved_runs else 0.0
    if last_saved_at:
        report.update(since=last_saved_at-86400, until=last_saved_at,
            window_basis="latest_retained_app_record",
            window_label="Last 24 hours ending at the last saved app record.")
    else:
        report.update(window_basis="review_time_no_saved_app_time",
            window_label="Last 24 hours ending at the review time; no valid saved app time was available.")
    anchor = saved_runs[-1][1] if saved_runs else {}
    anchor_diag = mapping(anchor.get("new_horizon_diagnostic_snapshot"))
    continuity = mapping(anchor_diag.get("referee_assessment_history")).get("continuity_key")
    anchor_session = mapping(mapping(mapping(anchor.get("current_state")).get("core_snapshot")).get("matrix_identity")).get("session_scope_key")
    source = Path(history_path).resolve() if history_path else portable_history_path(root)
    diagnostics, all_records, all_proofs, hours = [], [], [], []
    with tempfile.TemporaryDirectory(prefix="54guc-log-check-") as scratch:
        temp = Path(scratch)
        for index, (run, saved, run_ts) in enumerate(saved_runs):
            if running():
                raise AppRunning("54GUC restarted; the unfinished review was discarded.")
            diag = saved["new_horizon_diagnostic_snapshot"]
            scope = mapping(diag.get("referee_assessment_history")).get("continuity_key")
            session = mapping(mapping(saved["current_state"].get("core_snapshot")).get("matrix_identity")).get("session_scope_key")
            mixed = bool(continuity and scope != continuity)
            if not continuity:
                mixed = (session != anchor_session) if anchor_session else saved is not anchor
            if mixed:
                finding(report, "Missing evidence", SECTIONS[6], "A run belongs to another router/network context and was not merged.", run.name, code='foreign_run_excluded')
                continue
            if run_ts and run_ts < report["since"]:
                continue
            report["runs"].append(run.name)
            try:
                inspect_snapshots(report, run.name, saved)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                finding(report, "Missing evidence", SECTIONS[6], "A saved snapshot has unsupported fields; part of its review could not run.", run.name, code='snapshot_unsupported')
            diagnostics.append(diag)
            db = run / "cgf_evidence.sqlite3"
            try:
                dest = temp / f"events-{index}.sqlite3"
                snapshot_database(db, dest)
                events, status = read_cgf_evidence_events(dest, max_events=MAX_ROWS)
                if status["disposition"] != "captured":
                    raise ValueError(status["reason"])
                if len(events) == MAX_ROWS:
                    finding(report, "Missing evidence", SECTIONS[6], "The diagnostic record limit was reached; earlier events may be missing.", run.name, code='journal_limit')
                inspect_events(report, run.name, [e for e in events if report["since"] <= stamp(e.get("ts")) <= report["until"]])
                report["files"].append(dict(name=run.name + "/cgf_evidence.sqlite3", status="Owner decoded", records=len(events)))
            except INPUT_ERRORS as exc:
                finding(report, "Missing evidence", SECTIONS[6], "Diagnostic journal could not be read: " + type(exc).__name__, run.name, code='journal_unreadable')
            for path in sorted(run.iterdir()):
                if path.suffix not in (".log", ".jsonl", ".gz"):
                    continue
                try:
                    if path.stat().st_size > MAX_TEXT:
                        raise ValueError("text record limit")
                    opener = gzip.open if path.suffix == ".gz" else open
                    with opener(path, "rb") as stream:
                        body = stream.read(MAX_TEXT + 1)
                    if len(body) > MAX_TEXT:
                        raise ValueError("expanded text record limit")
                    lines = body.decode("utf-8-sig", errors="replace").splitlines()
                    errors = sum("Traceback (most recent call last)" in line or '"severity": "ERROR"' in line or "ERROR:" in line for line in lines)
                    report["files"].append(dict(name=run.name + "/" + path.name, status="Text scanned", lines=len(lines)))
                    if errors:
                        finding(report, "Warning", SECTIONS[0], f"{errors} error markers were retained in this text log; no cause is assumed from the marker alone.", path.name, code='text_errors')
                except INPUT_ERRORS as exc:
                    finding(report, "Missing evidence", SECTIONS[6], "Text log could not be fully read: " + type(exc).__name__, path.name, code='text_unreadable')
        select_report_mode(report)
        if continuity and report["device_mode"] != "conflicting":
            try:
                snap = temp / "history.sqlite3"
                snapshot_database(source, snap)
                all_records, all_proofs, hours, observations = read_history(
                    snap, continuity, report)
                report["files"].append(dict(name="new_horizon.sqlite3", status="Read-only snapshot; integrity OK"))
            except INPUT_ERRORS as exc:
                finding(report, "Missing evidence", SECTIONS[4], "History review is incomplete: " + type(exc).__name__ + ". The original history was not changed.", "new_horizon.sqlite3", code='history_incomplete')
        elif not continuity:
            finding(report, "Missing evidence", SECTIONS[4], "No confirmed learning identity is retained. History was not guessed or combined with another router.", "History", code='learning_identity_missing')
    terminals = inspect_commands(report, runtime, current_state=mapping(anchor.get("current_state")))
    make_decisions(all_records, all_proofs, report, command_terminals=terminals, session_scope=anchor_session)
    hourly_rows(report, all_proofs, hours, diagnostics)
    if not report["decisions"]:
        note(report, SECTIONS[4], "No submitted Brain-decision frame was retained in the review window; this does not prove Brain was inactive.")
    report["summary"] = ("Problems were recorded. Review the findings below." if any(f["level"] == "Problem" for f in report["findings"]) else
                         "Review incomplete: some evidence is missing. No confirmed problem was found in the checks that could run." if any(f["level"] == "Missing evidence" for f in report["findings"]) else
                         "No confirmed problem was found by the available automatic checks. Read the warnings and evidence limits.")
    if running():
        raise AppRunning("54GUC restarted; the unfinished review was discarded.")
    return report


def analyse_bundle(bundle_path, *, now=None, candidate_profile=None):
    """Analyse a frozen support ZIP directly; never restore an installation."""
    from arch4.foundation.support_archive import read_support_archive
    from arch4.support_evidence import COMMAND_SCHEMA, read_support_command
    from tools.log_checker.bundle_input import BundleHistory
    manifest, files = read_support_archive(bundle_path)
    prefix = "portable_runtime/guc_output/"
    snapshot = json.loads(files[prefix + "foundation_evidence_snapshot.json"])
    until = float(snapshot["captured_at"] if now is None else now)
    report = blank_report(until)
    window = manifest.get("intelligence_history", {})
    if window.get("included"):
        report["since"] = max(report["since"], float(window["from_at"]))
        report["until"] = min(until, float(window["to_at"]))
    name = Path(bundle_path).name
    saved = {n: {} for n in SNAPSHOTS}
    for n in SNAPSHOTS:
        if n in {"current_state", "runtime_health"}:
            saved[n] = mapping(snapshot.get(n))
        elif prefix + n + ".json" in files:
            saved[n] = mapping(json.loads(files[prefix + n + ".json"]))
    report["runs"] = [name]
    inspect_snapshots(report, name, saved, support=True)
    inspect_events(report, name, [e for e in snapshot["events"]
        if report["since"] <= stamp(e.get("ts")) <= report["until"]])
    if snapshot.get("complete") is not True or snapshot.get("omissions"):
        finding(report, "Missing evidence", SECTIONS[6],
                "The saved support capture is incomplete; missing inputs were not reconstructed.", name, code='capture_incomplete')
    report["limitations"].append("Diagnostic events are a bounded retained timeline, not proof of every event since launch.")
    core = mapping(saved["current_state"].get("core_snapshot"))
    session_scope = mapping(core.get("matrix_identity")).get("session_scope_key")
    diag = saved["new_horizon_diagnostic_snapshot"]
    continuity = mapping(diag.get("referee_assessment_history")).get("continuity_key")
    records, proofs, hours = [], [], []
    select_report_mode(report)
    if window.get("included") and continuity and report["device_mode"] != "conflicting":
        try:
            history = BundleHistory(files[prefix + window["member"]])
            records, proofs, hours, _ = read_history(history, continuity, report)
        except INPUT_ERRORS as exc:
            limits = {
                "support_history_record_bytes_limit": "an individual History record is too large",
                "support_history_record_count_limit": "too many History records",
                "support_history_retained_diagnostics_limit": "retained diagnostic memory budget exceeded",
            }
            finding(report, "Missing evidence", SECTIONS[4],
                    "Saved session History could not be fully read: "
                    + limits.get(str(exc), type(exc).__name__), name, code='bundle_history_unreadable')
    elif not continuity or not window.get("included"):
        finding(report, "Missing evidence", SECTIONS[4], "No qualified session learning History is included.", name, code='session_history_missing')
    commands = {}
    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if value.get("schema") == COMMAND_SCHEMA:
                row = read_support_command(value)
                if report["since"] <= row["terminal_ts"] <= report["until"]:
                    key = (row["transaction_root_id"], row["receipt_sequence"])
                    if key in commands and commands[key] != row:
                        finding(report, "Problem", SECTIONS[6], "Saved copies of a command outcome disagree.", name, code='command_copies_disagree')
                    commands[key] = row
            for item in value.values():
                collect(item)
    collect(snapshot)
    collect(saved["ngui_page_truth"])
    for row in records:
        collect(row)
    report["diagnostics"]["command_outcomes"] = dict(Counter(r["terminal_status"] for r in commands.values()))
    for row in sorted(commands.values(), key=lambda r:r["terminal_ts"]):
        note(report, SECTIONS[3], clock(row["terminal_ts"]) + ": " + safe(row["command"])
             + " — " + safe(row["terminal_status"]) + "; " + safe(row["reason"]) + ".")
        note(report, SECTIONS[3], command_explanation(row["terminal_status"]))
    make_decisions(records, proofs, report, command_terminals=commands.values(), session_scope=session_scope)
    hourly_rows(report, proofs, hours, [diag])
    report["files"] = [dict(name=n, status="Frozen ZIP read", bytes=len(b)) for n,b in files.items()]
    from tools.log_checker.profile_fields import inspect_bundle_fields
    report["profile_fields"] = inspect_bundle_fields(manifest, files, candidate=candidate_profile)
    report["summary"] = (
        "Problems were recorded. Review the findings below." if any(f["level"] == "Problem" for f in report["findings"]) else
        "Review incomplete: some evidence is missing. No confirmed problem was found in the checks that could run." if any(f["level"] == "Missing evidence" for f in report["findings"]) else
        "No confirmed problem was found by the available automatic checks. Read the warnings and evidence limits.")
    return report


def inspect_commands(report, runtime, *, current_state=None):
    """Command Authority's retained receipt is the only completion source."""
    validated = []
    state = read_json(runtime / "guc_memory/cgf_state/command_authority.json", report, SECTIONS[3], required=True)
    if state:
        terminal = state.get("terminal", [])
        if not isinstance(terminal, list):
            finding(report, "Missing evidence", SECTIONS[3], "Saved command outcomes are malformed.", "command_authority.json", code='command_outcomes_malformed')
            terminal = []
        if any(not isinstance(r, dict) or number(r.get("terminal_ts")) is None
               or clock(r.get("terminal_ts")) == "Invalid saved time" for r in terminal):
            finding(report, "Missing evidence", SECTIONS[3], "Saved command outcomes contain invalid timestamps or fields.", "command_authority.json", code='command_outcome_fields_invalid')
        recent = [r for r in terminal if isinstance(r, dict)
                  and report["since"] <= stamp(r.get("terminal_ts")) <= report["until"]]
        statuses = Counter(str(r.get("terminal_status", "not recorded")) for r in recent)
        report["diagnostics"]["command_outcomes"] = dict(statuses)
        for row in recent:
            note(report, SECTIONS[3], clock(row["terminal_ts"]) + ": " + safe(row.get("family", "command")) + " — " + safe(row.get("terminal_status", "not recorded")) + "; " + safe(row.get("reason", "Reason not recorded")) + ".")
            try:
                validated.append(CommandTerminalReceipt.from_mapping(row))
            except INPUT_ERRORS:
                finding(report, "Missing evidence", SECTIONS[3], "A command receipt failed canonical validation; its saved status is listed but cannot prove a Brain proposal completed.", "command_authority.json", code='command_receipt_invalid')
            note(report, SECTIONS[3], command_explanation(row.get("terminal_status")))
        current = (current_state if current_state is not None else
                   read_json(runtime / "guc_output/current_state.json", report, SECTIONS[5]))
        matrix = mapping(mapping(mapping(mapping(current.get("core_snapshot")).get("registers"))
                                 .get("modem_live_matrix")).get("value"))
        cell = mapping(mapping(mapping(mapping(mapping(matrix.get("lines")).get("radio"))
                               .get("cells")).get("configured_band_pool")))
        snapshot_at = number(mapping(current.get("core_snapshot")).get("published_at"))
        if snapshot_at is None:
            snapshot_at = number(current.get("published_at"))
        if cell.get("state") == "available" and number(cell.get("acquired_at")) is not None:
            for row in recent:
                identity = mapping(row.get("matrix_identity"))
                if not all(matrix.get(key) and matrix.get(key) == identity.get(key)
                           for key in ("source_epoch", "session_scope_key", "lifecycle_generation")):
                    continue
                stages = mapping(mapping(row.get("receipts")).get("modem")).get("stages", [])
                for stage in stages if isinstance(stages, list) else ():
                    stage = mapping(stage)
                    if (stage.get("stage_kind") in {"pband_anchor", "ca_pool", "band_selection", "band_lock", "band_auto"}
                            and stage.get("outcome") in {"accepted", "response_unavailable"}
                            and mapping(mapping(stage.get("target")).get("value")).get("domain") != "nr_approved_auto"
                            and stamp(cell.get("acquired_at")) < stamp(stage.get("request_dispatched_at"))):
                        if snapshot_at is not None and snapshot_at < stamp(row.get("terminal_ts")):
                            finding(report, "Missing evidence", SECTIONS[5],
                                "The device snapshot was saved before this action finished. "
                                "The final band settings cannot be checked using these two files together.",
                                "command_authority.json / current_state.json", code='pool_snapshot_older_than_result')
                        else:
                            finding(report, "Problem", SECTIONS[5],
                                "The current allowed-band pool predates a possibly applied band write and is still published as available. "
                                "The post-change readback did not reach current device truth.", "command_authority.json / current_state.json", code='pool_readback_stale')
                        break
        if state.get("active_cellular"):
            finding(report, "Warning", SECTIONS[3], "An active command was still present in the final saved Command Authority state; check the shutdown receipt and terminal records.", "command_authority.json", code='command_still_active')
    recovery = read_json(runtime / "guc_memory/cgf_state/internet_emergency_state.json", report, SECTIONS[2])
    if recovery:
        phase = recovery.get("phase")
        report["diagnostics"]["recovery_state"] = dict(phase=phase)
        if phase and phase != "armed":
            note(report, SECTIONS[2], "Recovery status: " + safe(phase) + ".")
            note(report, SECTIONS[2], recovery_explanation(phase))
    # Parent text logs are diagnostic markers only. They cannot override Core.
    for path in sorted((runtime / "launcher_logs").glob("*.log")):
        try:
            if path.stat().st_size > MAX_TEXT:
                raise ValueError("parent log limit")
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            count = sum("Traceback (most recent call last)" in line or "ERROR:" in line for line in lines)
            report["files"].append(dict(name="launcher_logs/" + path.name, status="Text scanned", lines=len(lines)))
            if count:
                finding(report, "Warning", SECTIONS[0], f"The retained launcher log has {count} error markers. Undated text may include earlier runs.", path.name, code='launcher_errors')
        except (OSError, ValueError) as exc:
            finding(report, "Missing evidence", SECTIONS[6], "A launcher log could not be fully read: " + type(exc).__name__, path.name, code='launcher_unreadable')
    return validated


def write_report(report, output):
    # Keep original evidence and severity; explanations add no new findings.
    report = scrub_record({k: v for k, v in report.items() if k != "brain_evaluations"})
    fields = mapping(report.get("profile_fields"))
    field_rows = fields.get("fields", [])
    for row in field_rows:
        row["user_explanation"] = field_explanation(row["status"])
        if row.get("candidate_status"):
            row["candidate_explanation"] = field_explanation(row["candidate_status"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    title = "54GUC Log Checker " + VERSION + " - FINAL"
    text = [title, "Created: " + clock(report["generated_at"]),
            "Window: " + clock(report["since"]) + " to " + clock(report["until"]),
            report.get("window_label", "The reviewed interval is shown above."),
            "Device review mode: " + report["device_mode"], report["summary"], ""]
    esc = html.escape
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>" + esc(title) + "</title>",
        "<style>body{font:16px system-ui;max-width:1200px;margin:2em auto;padding:0 1em;color:#203040;line-height:1.5}table{border-collapse:collapse;width:100%}th,td{border:1px solid #cdd3da;padding:.65em;text-align:left;vertical-align:top}th{background:#eef3f8}li{margin:.6em 0}.finding{padding:1em;border:1px solid #ccd4df;border-left:5px solid #54718c;border-radius:6px;margin:1em 0}.Problem{border-left-color:#a21c1c}.Warning{border-left-color:#a76700}small{color:#52606b}td{min-width:9em;overflow-wrap:anywhere;white-space:pre-line}details{margin:.7em 0}summary{cursor:pointer;font-weight:600}p{margin:.5em 0}.table-wrap{overflow:auto}</style></head><body>",
        "<h1>" + esc(title) + "</h1><p>" + esc(clock(report["since"]) + " to " + clock(report["until"])) + "</p>",
        "<p>Created: " + esc(clock(report["generated_at"])) + "</p><p>" + esc(report.get("window_label", "The reviewed interval is shown above.")) + "</p>",
        "<p>Device review mode: " + esc(report["device_mode"]) + "</p><h2>" + esc(report["summary"]) + "</h2>"]
    if report["findings"]:
        key = "Problem = a recorded inconsistency or fault. Warning = an event to review. Missing evidence = this check could not be completed."
        text.extend([key, ""])
        parts.append("<p>" + esc(key) + "</p>")
    for f in report["findings"]:
        text.extend([f["level"] + " - " + f["title"] + " (" + f["section"] + ")",
            "What happened: " + f["meaning"], "Why it matters: " + f["impact"],
            "What to do: " + f["next_step"],
            "Recorded detail: " + f["text"] + " [" + f["source"] + "]", ""])
        parts.append("<article class='finding " + esc(f["level"]) + "'><h3>" + esc(f["level"] + ": " + f["title"]) + "</h3>" +
            "<p><b>What happened:</b> " + esc(f["meaning"]) + "</p><p><b>Why it matters:</b> " + esc(f["impact"]) +
            "</p><p><b>What to do:</b> " + esc(f["next_step"]) + "</p><details><summary>Recorded detail</summary><p>" +
            esc(f["text"]) + "</p><small>" + esc(f["section"] + " / " + f["source"] + " / " + f["code"]) + "</small></details></article>")
    for section, lines in report["sections"].items():
        if lines:
            text.extend(["", section.upper(), *lines])
            parts.append("<h2>" + esc(section) + "</h2><ul>" + "".join("<li>" + esc(line) + "</li>" for line in lines) + "</ul>")
    if report.get('field_availability'):
        text.extend(['', 'SAVED INPUT AVAILABILITY',
                     'Availability states are not bug counts. These are saved snapshots, not a complete availability timeline.'])
        parts.append('<h2>Saved input availability</h2><p>Availability states are not bug counts. These are saved snapshots, not a complete availability timeline.</p>')
        for source, view in report['field_availability'].items():
            text.append(source)
            parts.append('<h3>' + esc(source) + '</h3><div class="table-wrap"><table><tr><th>Unavailable field</th><th>State</th><th>Recorded reason</th></tr>')
            for row in view['fields']:
                if row['state'] == 'available':
                    continue
                values = (row['field'], row['state'], row['reason'])
                text.append(' / '.join(values))
                parts.append('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in values) + '</tr>')
            parts.append('</table></div>')
    if field_rows:
        text.extend(["", "ROUTER FIELDS AND PROFILE RULES", *fields.get("limits", [])])
        parts.append("<details><summary>Router fields and profile rules</summary><ul>" +
            "".join("<li>" + esc(line) + "</li>" for line in fields.get("limits", [])) +
            "</ul><div class='table-wrap'><table><tr><th>Field</th><th>Result and explanation</th><th>Value / unit</th><th>Profile rule</th></tr>")
        for row in field_rows:
            e = row["user_explanation"]
            result = row["status"] + "\nWhat happened: " + e["meaning"] + "\nWhat to do: " + e["next_step"]
            if "candidate_status" in row or "candidate_value" in row:
                result += "\nCandidate: " + str(row.get("candidate_status", row.get("candidate_value")))
                if row.get("candidate_explanation"):
                    result += "\n" + row["candidate_explanation"]["meaning"] + "\nWhat to do: " + row["candidate_explanation"]["next_step"]
            label = str(row["id"]) + ": " + row["field"]
            value = str(row.get("value", "Not available")) + " " + (row.get("unit") or "")
            rule = row["rule_pointer"] + "\n" + (row.get("rule") or "No direct native rule")
            text.extend([label + " - " + result + "; " + value + "; " + rule])
            parts.append("<tr>" + "".join("<td>" + esc(v) + "</td>" for v in (label, result, value, rule)) + "</tr>")
        parts.append("</table></div></details>")
    text.extend(["", "BRAIN'S OPTIONS AND OUTCOMES - LAST 24 HOURS",
        "No recorded decision is not a decision to stay. Option scores are only shown when saved at that time."])
    parts.append("<h2>Brain's options and outcomes - last 24 hours</h2><p>Each row uses this PC's local clock. A blank hour means not recorded. A snapshot describes its own time. Other options' scores are not rebuilt from later History.</p><div class='table-wrap'><table><tr><th>PC local time</th><th>Observed configuration / AQ</th><th>Recorded options</th><th>Brain selected / why</th><th>Request result</th></tr>")
    labels = (("actual", "Observed configuration / AQ"), ("options", "Recorded options"),
              ("selected", "Brain selected / why"), ("results", "Request result"))
    for row in report["hourly"]:
        text.extend(["", row["interval"]])
        for key, label in labels:
            text.append(label + ": " + "\n  ".join(row[key] or ["Not recorded"]))
        parts.append("<tr><td>" + esc(row["interval"]) + "</td>" +
            "".join("<td>" + "<hr>".join(esc(v) for v in row[key] or ["Not recorded"]) + "</td>" for key, _ in labels) + "</tr>")
    parts.append("</table></div>")
    text.extend(["", "READING THIS REPORT", "AQ is the saved service-quality grade; higher values on the same grading scale are better. A forecast is an estimate; it is not a measured result.",
        "CQ includes bandwidth. CQB excludes bandwidth; Referee averages CQB over its assessment window and grades capacity separately. A CQ percentage and a Referee CQB fraction are different quantities. No quality grades are recalculated by this checker.",
        "LTE is 4G. NR is 5G. An allowed band list permits bands; it does not mean all those bands are connected at once.",
        "PCI identifies a nearby radio cell. The cell ID and channel supply additional context; PCI alone is not a unique tower identity.",
        "Fresh means newly read. Retained means an older reading was deliberately kept; it is not a new measurement.",
        "", "LIMITS", *report["limitations"], "", "FILES CHECKED"])
    parts.append("<details><summary>Reading this report and evidence limits</summary><ul>" +
        "".join("<li>" + esc(line) + "</li>" for line in text[text.index("READING THIS REPORT")+1:text.index("LIMITS")-1]) +
        "</ul><ul>" + "".join("<li>" + esc(line) + "</li>" for line in report["limitations"]) + "</ul></details>")
    text.extend(f["name"] + ": " + f["status"] for f in report["files"])
    parts.append("<details><summary>Files checked</summary><ul>" +
        "".join("<li>" + esc(f["name"] + ": " + f["status"]) + "</li>" for f in report["files"]) + "</ul></details></body></html>")
    (output / "report.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    (output / "report.html").write_text("\n".join(parts), encoding="utf-8")
    return output / "report.html"
