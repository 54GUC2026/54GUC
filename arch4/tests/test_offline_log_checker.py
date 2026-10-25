"""Stopped-app behavior, deceptive evidence and read-only boundary scenarios."""
import ast
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import pytest

from tools.log_checker import report as checker
from tools.log_checker.app import SingleChecker, main
from arch4.foundation.runtime_artifacts import CgfEvidenceStore
from arch4.new_horizon.history import AdvisoryHistory
from arch4.new_horizon.contracts import DECISION_LEDGER_KIND, DECISION_LEDGER_SCHEMA
from arch4.core_contracts.intelligence_proposals import GRADE_DIMENSIONS
from arch4.core_contracts.action_reachability import (
    CoreCurrentReachabilityView, CurrentReachabilityDisposition,
)


NOW = datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc).timestamp()


def device_facts(run, nr_status, *, network_type="LTE (4G)", nr_active=False):
    from arch4.architecture_contracts.runtime_generation import (
        MatrixGenerationIdentity, RuntimeGenerationNamespace,
    )
    from arch4.core_contracts.accepted_current_facts import (
        CoreAcceptedCurrentFact, CoreAcceptedCurrentFacts,
    )
    from arch4.core_contracts.payload import CoreFrozenPayload
    identity = MatrixGenerationIdentity(
        RuntimeGenerationNamespace("router-one", "epoch-one", 1), 5, "a" * 64)
    values = {"capability.session_nr_status": nr_status,
              "capability.device_radio_class": {"unsupported":"4g_only", "seeded":"5g", "proven":"5g"}.get(nr_status,"unclassified"),
              "radio.network_type": network_type, "radio.nr5g_active": nr_active,
              "rf.pci": 102, "rf.sinr_db": 7.5,
              "radio.nr_pci": 12, "radio.nr_sinr_db": 21.5}
    facts = tuple(CoreAcceptedCurrentFact(
        line=path.split(".")[0], field=path.split(".")[1],
        disposition="available", value=CoreFrozenPayload.from_mapping(
            {"present": True, "value": value}), reason="", sampled_at=float(NOW),
        acquired_at=float(NOW), acquisition_attempt_id="read-one",
        source_lane="startup_full", acquisition_namespace=identity.namespace,
        retention=None, line_sequence=5, source_sequence=5, source="startup_full")
        for path, value in values.items())
    projection = CoreAcceptedCurrentFacts.prepare(matrix_identity=identity, facts=facts)
    put(run / "current_state.json", dict(core_snapshot=dict(
        matrix_identity=identity.as_dict(), accepted_current_facts=projection.to_payload()),
        core_current_health=dict(modem_live_fast_lane=dict(miss_count=0, stale=False))))


@pytest.mark.parametrize("nr_status,network,nr_active,mode", [
    ("unsupported", "LTE (4G)", False, "4G"),
    ("seeded", "LTE (4G)", False, "5G"),
    ("proven", "NR NSA (5G)", True, "5G"),
    ("proven", "NR SA (5G)", True, "5G"),
    ("proven", "LTE (4G)", False, "5G"),
])
def test_checker_mode_uses_recorded_device_capability_not_current_rat(
        tmp_path, nr_status, network, nr_active, mode):
    root, run = stage(tmp_path)
    device_facts(run, nr_status, network_type=network, nr_active=nr_active)
    before = (run / "current_state.json").read_bytes()
    result = checker.analyse(root, now=NOW)
    assert result["device_mode"] == mode
    assert result["device_modes"][run.name]["mode"] == mode
    assert result["device_modes"][run.name]["nr_active"] is nr_active
    assert (run / "current_state.json").read_bytes() == before
    if mode == "5G":
        readings = result["device_modes"][run.name]["readings"]
        assert readings["rf.pci"]["value"] == 102
        assert readings["radio.nr_pci"]["value"] == 12


@pytest.mark.parametrize("problem", ["missing", "unknown", "bad_digest", "different_generation"])
def test_checker_unknown_class_never_guesses_from_n78_or_model(tmp_path, problem):
    root, run = stage(tmp_path)
    if problem != "missing":
        device_facts(run, "unknown" if problem == "unknown" else "proven",
                     network_type="NR NSA (5G)", nr_active=True)
        body = json.loads((run / "current_state.json").read_text())
        if problem == "bad_digest":
            body["core_snapshot"]["accepted_current_facts"]["facts_digest"] = "b" * 64
        if problem == "different_generation":
            body["core_snapshot"]["matrix_identity"]["source_epoch"] = "another-epoch"
        put(run / "current_state.json", body)
    result = checker.analyse(root, now=NOW)
    assert result["device_mode"] == "unclassified"
    assert any("device class" in item["text"] for item in result["findings"])


def test_checker_conflicting_classes_do_not_merge_learning(tmp_path, monkeypatch):
    root, run = stage(tmp_path)
    device_facts(run, "proven")
    other = root / "portable_runtime/guc_output_20260919_121000"
    other.mkdir()
    for path in run.iterdir():
        if path.suffix == ".json":
            (other / path.name).write_bytes(path.read_bytes())
    device_facts(other, "unsupported")
    monkeypatch.setattr(checker, "read_history", lambda *args: pytest.fail("mixed learning read"))
    result = checker.analyse(root, now=NOW)
    assert result["device_mode"] == "conflicting"
    assert any(item["level"] == "Problem" and "device classes" in item["text"]
               for item in result["findings"])
    assert not result["decisions"]


def test_checker_extended_service_grade_and_rules_grade_remain_distinct():
    assert checker.proof_grade_text(dict(dimension="service_outcome", grade=4.0,
        status="graded", final_assessment=True)) == "; AQ +4.0"
    assert checker.proof_grade_text(dict(dimension="rules_safety", grade=3.0,
        status="graded", final_assessment=True)) == "; Rules/safety +3.0"


def test_checker_5g_explanation_does_not_assign_legacy_bonus_to_all_models():
    report = checker.blank_report(NOW)
    report["device_modes"] = {"run": {"mode": "5G"}}
    checker.select_report_mode(report)
    text = json.dumps(report)
    assert "5g-combined-v1" in text
    assert "-3 to +3" in text
    assert "Older records retain their recorded grading scale" in text
    assert "can reach +4" not in text


@pytest.mark.parametrize("mode", ["4G", "5G", "unclassified", "conflicting"])
def test_saved_checker_outputs_identify_the_selected_review_mode(tmp_path, mode):
    report = checker.blank_report(NOW)
    report["device_mode"] = mode
    report["summary"] = "Saved evidence reviewed."
    checker.write_report(report, tmp_path / "review")
    for filename in ("report.txt", "report.html"):
        text = (tmp_path / "review" / filename).read_text(encoding="utf-8")
        assert "Device review mode: " + mode in text


def test_checker_mode_owner_cannot_be_replaced_by_profile_or_transient_rat():
    import inspect
    text = inspect.getsource(checker.recorded_device_mode)
    assert "CoreAcceptedCurrentFacts.from_payload" in text
    assert 'exact("capability.device_radio_class")' in text
    assert 'session_nr_status' not in text
    assert "catalog" not in text and "get_modem_profile" not in text
    before_selection = text.split('mode =')[0]
    assert '"radio.nr5g_active"' not in before_selection
    assert '"radio.network_type"' not in before_selection


def owner_outage():
    return CoreCurrentReachabilityView(session_scope_key="test-router",
        identity_code="test-identity", observer_id="modem_live_rtt",
        register_sequence=1, observed_at=NOW, latest_probe_at=NOW,
        disposition=CurrentReachabilityDisposition.OUTAGE,
        outage_started_at=NOW-8, outage_s=8, max_probe_gap_s=5).as_dict()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def stage(tmp_path, *, scope="physical-one", fault_count=0, coherent=True):
    root = tmp_path / "app"
    root.mkdir(exist_ok=True)
    (root / "54GUC.exe").write_bytes(b"fixture, not executable")
    run = root / "portable_runtime" / "guc_output_20260919_120000"
    put(run / "runtime_health.json", dict(ts=NOW, faults=dict(fault_count=fault_count), workers=dict(failed=False)))
    put(run / "current_state.json", dict(core_current_health=dict(modem_live_fast_lane=dict(miss_count=0, stale=False))))
    put(run / "shutdown_receipt.json", dict(status="complete"))
    put(run / "new_horizon_diagnostic_snapshot.json", dict(captured_at=NOW-1,
        referee_assessment_history=dict(continuity_key=scope),
        learning_population=dict(coherent=coherent, source_assessment_count=12, learned_assessment_count=2,
                                 learning_population_id="a", forecast_population_id="a" if coherent else "b"),
        history=dict(log_processor=dict(flush_failures=0, critical_loss_count=0, routine_loss_count=0))))
    journal = CgfEvidenceStore(run)
    journal.log_event("core_internet_timeline_change", current=dict(disposition="reachable"))
    journal.close()
    return root, run


def decision(at=NOW-300, pid="p1"):
    return dict(kind=DECISION_LEDGER_KIND, scope_key="physical-one", event_key="decision:"+pid,
                observed_at=at, payload=dict(schema=DECISION_LEDGER_SCHEMA, proposal_id=pid,
        source_signature="PBAND=B1|CA=B1+B3+B20", forecast=dict(status="predicted", expected_service_value=2.1),
        predicted_grades=dict(basis="prediction", components=[dict(dimension=d, grade=None, confidence=0.0, reason="unproven") for d in GRADE_DIMENSIONS]),
        explanation="Better recorded quality for this hour.", decision_frame=dict(candidate_ids=["do_nothing", "learned_lte:PBAND=B3|CA=B1+B3+B20"], accepted_at=at)))


def history(tmp_path, rows):
    path = tmp_path / "new_horizon.sqlite3"
    owner = AdvisoryHistory(path)
    for r in rows:
        owner.record(kind=r["kind"], scope_key=r["scope_key"], event_key=r["event_key"], observed_at=r["observed_at"], payload=r["payload"])
    owner.close()
    return path


def test_no_logs_is_incomplete_not_healthy(tmp_path):
    r = checker.analyse(tmp_path, now=NOW)
    assert "incomplete" in r["summary"]
    assert all(not h["selected"] for h in r["hourly"])
    assert abs(sum(h["end"]-h["start"] for h in r["hourly"])-86400) < .1


def test_live_app_no_reads_or_output(tmp_path):
    with pytest.raises(checker.AppRunning):
        checker.analyse(tmp_path, now=NOW, running=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_restart_discards_review(tmp_path):
    root, _ = stage(tmp_path)
    answers = iter([False, True])
    with pytest.raises(checker.AppRunning):
        checker.analyse(root, now=NOW, running=lambda: next(answers))
    assert not (root / "log_analysis").exists()


@pytest.mark.parametrize("fault,coherent,expected", [(0,True,False),(1,True,True),(0,False,True)])
def test_recorded_failures_not_hidden_by_clean_shutdown(tmp_path, fault, coherent, expected):
    root, _ = stage(tmp_path, fault_count=fault, coherent=coherent)
    r = checker.analyse(root, now=NOW)
    assert any(f["level"] == "Problem" for f in r["findings"]) == expected


@pytest.mark.parametrize("shutdown_status", ["complete", "degraded"])
@pytest.mark.parametrize("failed,expect_warning", [([],False),(["clock"],True)])
def test_shutdown_stopped_workers_not_false_runtime_failure(tmp_path, failed, expect_warning, shutdown_status):
    root, run = stage(tmp_path)
    put(run / "shutdown_receipt.json", dict(status=shutdown_status))
    put(run / "runtime_health.json", dict(ts=NOW, status_phase="shutdown", phase="shutdown_complete", degraded=False,
        workers=dict(failed=True, degraded=False, required_stopped=["clock"], required_failed=failed, optional_failed=[])))
    r = checker.analyse(root, now=NOW)
    assert any("failed worker" in f["text"] for f in r["findings"]) == expect_warning
    assert any("clean completion" in f["text"] for f in r["findings"]) == (shutdown_status != "complete")


@pytest.mark.parametrize("body", [b"{broken", b"[]", b"null", b"\xff"])
def test_corrupt_snapshot_is_visible(tmp_path, body):
    root, run = stage(tmp_path)
    (run / "runtime_health.json").write_bytes(body)
    r = checker.analyse(root, now=NOW)
    assert any(f["source"] == "runtime_health.json" for f in r["findings"])


def test_missing_explicit_history_never_uses_default(tmp_path):
    root, _ = stage(tmp_path)
    r = checker.analyse(root, now=NOW, history_path=tmp_path / "does-not-exist.sqlite3")
    assert any("History review is incomplete" in f["text"] for f in r["findings"])
    assert not (tmp_path / "does-not-exist.sqlite3").exists()


def test_corrupt_database_remains_byte_identical(tmp_path):
    root, _ = stage(tmp_path)
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"broken database")
    checker.analyse(root, now=NOW, history_path=bad)
    assert bad.read_bytes() == b"broken database"


def test_real_history_decoder_and_no_mutation(tmp_path):
    root, _ = stage(tmp_path)
    p = history(tmp_path, [decision()])
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    r = checker.analyse(root, now=NOW, history_path=p)
    assert len(r["decisions"]) == 1
    assert "Band B3; pool B1+B3+B20" in r["decisions"][0]["options"]
    assert "not retained" in r["decisions"][0]["selected"]
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest


def test_other_device_rows_are_not_used(tmp_path):
    root, _ = stage(tmp_path)
    row = decision()
    row["scope_key"] = "another-router"
    p = history(tmp_path, [row])
    r = checker.analyse(root, now=NOW, history_path=p)
    assert not r["decisions"]
    assert any("match this router" in f["text"] for f in r["findings"])


def test_other_run_not_merged(tmp_path):
    root, run = stage(tmp_path)
    old = root / "portable_runtime" / "guc_output_20260919_100000"
    put(old / "runtime_health.json", dict(ts=NOW-300, faults=dict(fault_count=10)))
    put(old / "new_horizon_diagnostic_snapshot.json", dict(referee_assessment_history=dict(continuity_key="other")))
    r = checker.analyse(root, now=NOW)
    assert r["runs"] == [run.name]
    assert not any(f["level"] == "Problem" for f in r["findings"])


def test_intelligence_off_does_not_guess_history(tmp_path):
    root, run = stage(tmp_path)
    (run / "new_horizon_diagnostic_snapshot.json").unlink()
    r = checker.analyse(root, now=NOW)
    assert any("Brain may have been off" in f["text"] for f in r["findings"])
    assert not any(f["level"] == "Problem" for f in r["findings"])


def test_real_compressed_journal_is_decoded(tmp_path, monkeypatch):
    # Both snapshots and journal must fall within the same retained day.
    monkeypatch.setattr("arch4.foundation.runtime_artifacts.time.time", lambda: NOW)
    root, run = stage(tmp_path)
    journal = CgfEvidenceStore(run)
    journal.log_event("core_internet_timeline_change", current=owner_outage())
    journal.close()
    r = checker.analyse(root, now=NOW+1)
    assert any(f["status"] == "Owner decoded" for f in r["files"])
    assert r["diagnostics"]["event_counts"][run.name]["net_down"] == 1
    assert any("1 Internet disconnect records" in s for s in r["sections"][checker.SECTIONS[2]])


def test_wal_snapshot_contains_committed_rows(tmp_path):
    src = tmp_path / "wal.sqlite3"
    with sqlite3.connect(src) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE demo(x)")
        con.execute("INSERT INTO demo VALUES(7)")
        con.commit()
        checker.snapshot_database(src, tmp_path / "copy.sqlite3")
        with sqlite3.connect(tmp_path / "copy.sqlite3") as copy:
            assert copy.execute("SELECT x FROM demo").fetchall() == [(7,)]


def test_sqlite_handles_closed_without_garbage_collection(tmp_path, monkeypatch):
    import gc
    source = tmp_path / "source.sqlite3"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE demo(x)")
    con.commit()
    con.close()
    opened = []
    connect = sqlite3.connect
    def tracked(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn
    monkeypatch.setattr(checker.sqlite3, "connect", tracked)
    gc.disable()
    try:
        checker.snapshot_database(source, tmp_path / "copy.sqlite3")
        for conn in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        (tmp_path / "copy.sqlite3").unlink()
    finally:
        gc.enable()


@pytest.mark.parametrize("valid", [True, False])
def test_checkpoint_reader_closes_on_success_and_failure(tmp_path, monkeypatch, valid):
    source = tmp_path / "history.sqlite3"
    con = sqlite3.connect(source)
    if valid:
        con.execute("CREATE TABLE checkpoint(scope_key,updated_at,payload,name)")
    con.commit()
    con.close()
    opened = []
    connect = sqlite3.connect
    def tracked(*args, **kwargs):
        c = connect(*args, **kwargs)
        opened.append(c)
        return c
    monkeypatch.setattr(sqlite3, "connect", tracked)
    if valid:
        assert checker.export_history_checkpoints(source, name="test") == ()
    else:
        with pytest.raises(sqlite3.OperationalError):
            checker.export_history_checkpoints(source, name="test")
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
    source.unlink()


def test_all_decisions_in_hour_not_only_last():
    r = checker.blank_report(NOW)
    rows = [decision(NOW-400, "one"), decision(NOW-100, "two")]
    checker.make_decisions(rows, [], r)
    checker.hourly_rows(r, [], [], [])
    assert len(r["decisions"]) == 2
    assert sum(len(h["selected"]) for h in r["hourly"]) == 2


def test_duplicate_proposal_not_counted_twice():
    r = checker.blank_report(NOW)
    checker.make_decisions([decision(), decision()], [], r)
    assert len(r["decisions"]) == 1


def test_later_forecast_not_backfilled():
    r = checker.blank_report(NOW)
    d = dict(captured_at=NOW-1, adviser_chain=dict(status="complete", candidates=[dict(opportunity_id="lte_anchor:B3", crystal_ball=dict(expected_service_value=2.8))], selected_opportunity_id="do_nothing"))
    checker.hourly_rows(r, [], [], [d])
    assert sum(bool(h["options"]) for h in r["hourly"]) == 1
    assert all(not h["options"] for h in r["hourly"][:-1])


def test_missing_frame_never_recomputed():
    r = checker.blank_report(NOW)
    d = decision()
    del d["payload"]["decision_frame"]
    checker.make_decisions([d], [], r)
    assert r["decisions"][0]["options"] == []


def test_wrong_cell_no_blame():
    r = checker.blank_report(NOW)
    d = decision()
    identity = dict(network_type="LTE (4G)", pband=3, cell_id=2050, pci=0, earfcn=1795)
    d["payload"]["forecast"].update(result_contexts=[dict(expected_aq=2.1, hourly_evidence=[dict(identity=identity)])])
    proof = dict(payload=dict(decision_id="p1", result_identity={**identity,"cell_id":2070}, status="graded", grade=-1.0,
                              dimension="service_outcome",final_assessment=True))
    checker.make_decisions([d], [proof], r)
    assert "not a failed Brain prediction" in r["decisions"][0]["result"]


@pytest.mark.parametrize("decision_id,final,status,warning", [
    ("core-command:p1", True, "graded", False),
    ("p1", True, "graded", False),
    ("core-command:p1", False, "graded", True),
    ("core-command:other", True, "graded", True),
    ("core-command:p1", True, "pending", True),
])
def test_final_core_command_proof_closes_offline_decision(decision_id, final, status, warning):
    r = checker.blank_report(NOW)
    proof = dict(payload=dict(decision_id=decision_id, subject_kind="core_command",
                              dimension="service_outcome",
                              final_assessment=final, status=status, grade=2.0,
                              result_identity=dict(pband=3, cell_id=2050)))
    checker.make_decisions([decision(NOW-800)], [proof], r)
    assert any(f["section"] == checker.SECTIONS[3] and "terminal" in f["text"]
               for f in r["findings"]) == warning


def test_explicit_failure_terminal_closes_offline_decision():
    r = checker.blank_report(NOW)
    terminal = dict(kind="decision_terminal_v2", payload=dict(proposal_id="p1", reason="target_rejected"),
                    observed_at=NOW-100)
    checker.make_decisions([decision(NOW-800), terminal], [], r)
    assert "target rejected" in r["decisions"][0]["result"]
    assert not any(f["section"] == checker.SECTIONS[3] and f["level"] == "Missing evidence"
                   for f in r["findings"])


def test_not_submitted_result_closes_offline_decision():
    r = checker.blank_report(NOW)
    result = dict(kind="proposal_result", payload=dict(proposal_id="p1", submitted=False),
                  observed_at=NOW-100)
    checker.make_decisions([decision(NOW-800), result], [], r)
    assert r["decisions"][0]["result"] == "Not submitted"
    assert not any(f["section"] == checker.SECTIONS[3] and f["level"] == "Missing evidence"
                   for f in r["findings"])


@pytest.mark.parametrize("pband,pci", [(1,0),(3,255),(20,None)])
def test_cell_and_zero_pci_preserved(pband, pci):
    text = checker.context_text(dict(pband=pband,ca_pool=[1,3,20],cell_id=2080,pci=pci))
    assert "Cell 2080" in text
    assert ("PCI 0" in text) == (pci == 0)


def test_html_escaped_and_plain_text_available(tmp_path):
    r = checker.blank_report(NOW)
    r["summary"] = "<script>bad</script>"
    checker.hourly_rows(r, [], [], [])
    out = tmp_path / "report"
    checker.write_report(r, out)
    assert "<script>" not in (out / "report.html").read_text()
    assert (out / "report.txt").exists() and (out / "report.json").exists()
    with pytest.raises(FileExistsError):
        checker.write_report(r, out)


@pytest.mark.parametrize("phase", [None, "armed"])
def test_user_report_omits_empty_internet_commands_and_boilerplate(tmp_path, phase):
    root, _ = stage(tmp_path)
    state = root / "portable_runtime/guc_memory/cgf_state"
    put(state / "command_authority.json", dict(terminal=[]))
    if phase:
        put(state / "internet_emergency_state.json", dict(phase=phase))
    r = checker.analyse(root, now=NOW)
    out = tmp_path / "review"
    checker.write_report(r, out)
    for suffix in ("html", "txt"):
        text = (out / ("report." + suffix)).read_text(encoding="utf-8")
        for noise in ("INTERNET AND RECOVERY", "COMMANDS", "saved NET-down",
                      "recovery-related records", "command-related diagnostic",
                      "Saved RTT statuses", "Last saved recovery state",
                      "Saved Command Authority outcomes", "No further saved evidence"):
            assert noise.casefold() not in text.casefold()
    saved = json.loads((out / "report.json").read_text())
    assert saved["diagnostics"]["event_counts"]
    assert saved["diagnostics"]["command_outcomes"] == {}
    if phase:
        assert saved["diagnostics"]["recovery_state"]["phase"] == phase


def test_user_report_keeps_real_outage_recovery_command_and_missing_evidence(tmp_path):
    r = checker.blank_report(NOW)
    r["summary"] = "Review incomplete."
    outage = owner_outage()
    outage["latest_completed_outage"] = dict(started_at=NOW-80, duration_s=70.8)
    checker.inspect_events(r, "saved-run", [
        dict(kind="core_internet_timeline_change", data=dict(current=outage)),
        dict(kind="emergency_takeover"), dict(kind="command_dispatched")])
    state = tmp_path / "runtime/guc_memory/cgf_state"
    put(state / "command_authority.json", dict(terminal=[
        dict(terminal_ts=NOW-10, family="band selection", terminal_status="failed",
             reason="Router rejected request")]))
    put(state / "internet_emergency_state.json", dict(phase="takeover"))
    checker.inspect_commands(r, tmp_path / "runtime")
    checker.write_report(r, tmp_path / "review")
    for suffix in ("html", "txt"):
        text = (tmp_path / "review" / ("report." + suffix)).read_text()
        assert "70.8 seconds" in text
        assert "Recovery status: takeover" in text
        assert "band selection" in text and "failed" in text
        assert "Missing evidence" in text and "canonical validation" in text
        assert "Saved Command Authority outcomes" not in text


def test_user_report_times_use_only_pc_local_clock(tmp_path):
    r = checker.blank_report(NOW)
    r["summary"] = "Saved evidence reviewed."
    checker.hourly_rows(r, [], [], [])
    local = datetime.fromtimestamp(NOW).strftime("%d %b %Y %H:%M:%S")
    assert checker.clock(NOW) == local
    assert r["hourly"][0]["hour"].endswith(":00")
    checker.write_report(r, tmp_path / "review")
    for suffix in ("html", "txt"):
        text = (tmp_path / "review" / ("report." + suffix)).read_text()
        assert local in text
        assert not re.search(r"\bUTC\b|[+-]\d{4}\b", text)
        assert "offset" not in text
    saved = json.loads((tmp_path / "review/report.json").read_text())
    assert saved["until"] == NOW
    assert sum(h["end"]-h["start"] for h in saved["hourly"]) == 86400


def test_no_credentials_file_read(tmp_path):
    root, run = stage(tmp_path)
    put(run / "license_credentials.json", dict(password="secret-value-no-report"))
    r = checker.analyse(root, now=NOW)
    assert "secret-value-no-report" not in json.dumps(r)
    assert not any("license" in f["name"] for f in r["files"])


def test_gzip_error_log_scanned(tmp_path):
    root, run = stage(tmp_path)
    with gzip.open(run / "runtime.log.gz", "wb") as stream:
        stream.write(b"Traceback (most recent call last)\n")
    r = checker.analyse(root, now=NOW)
    assert any("error markers" in f["text"] for f in r["findings"])


def test_window_edges_and_midnight():
    for now in [NOW, NOW-12*3600, NOW+11.5*3600]:
        r = checker.blank_report(now)
        checker.hourly_rows(r, [], [], [])
        assert r["hourly"][0]["start"] == now-86400
        assert r["hourly"][-1]["end"] == now
        assert all(a["end"] == b["start"] for a,b in zip(r["hourly"],r["hourly"][1:]))


def test_checker_lock_released(tmp_path):
    a = SingleChecker(tmp_path)
    with pytest.raises(RuntimeError):
        SingleChecker(tmp_path)
    a.close()
    SingleChecker(tmp_path).close()


def test_frozen_selftest_reads_no_app(tmp_path):
    assert main(["--self-test", str(tmp_path / "smoke")]) == 0
    assert "Synthetic" in (tmp_path / "smoke/report.txt").read_text()


def test_static_read_only_canonical_owner_boundary():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "tools/log_checker/report.py").read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert {"arch4.new_horizon.history", "arch4.new_horizon.decision_history", "arch4.foundation.runtime_artifacts"} <= imports
    assert not any(s and any(k in s for k in ("modem_live", "advisory", "reasoning", "windows_native_launcher")) for s in imports)
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
    assert not calls & {"AdvisoryHistory", "AdvisoryEngine", "eval", "exec"}
    sql = [n.value.upper() for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
    assert not any(s.startswith(("UPDATE ","DELETE ","INSERT ","CREATE ","DROP ")) for s in sql)


@pytest.mark.parametrize("same,screen,problem", [(True, True, False), (True, False, True), (False, False, False)])
def test_forensic_net_comparison_requires_same_generation(tmp_path, same, screen, problem):
    """Compare stopped diagnostic artifacts; never feed them into live NGUI."""
    root, run = stage(tmp_path)
    put(run / "current_state.json", dict(core_snapshot=dict(matrix_identity=dict(matrix_sequence=10, source_epoch=2),
        registers=dict(internet_reachability=dict(value=dict(disposition="reachable"))))))
    put(run / "ngui_page_truth.json", dict(chart_delivery=dict(front_source_sequence=10 if same else 9, front_source_epoch=2),
        fields={"quality.internet_reachable": dict(value=screen)}))
    r = checker.analyse(root, now=NOW)
    assert any(f["section"] == "Screen consistency" and f["level"] == "Problem" for f in r["findings"]) == problem


@pytest.mark.parametrize("same,screen,problem", [(True, False, False), (True, True, True), (False, True, False)])
def test_owner_outage_is_counted_and_compared_without_inventing_net(tmp_path, same, screen, problem):
    root, run = stage(tmp_path)
    outage = owner_outage()
    put(run / "current_state.json", dict(core_snapshot=dict(matrix_identity=dict(matrix_sequence=10, source_epoch=2),
        registers=dict(internet_reachability=dict(value=outage)))))
    put(run / "ngui_page_truth.json", dict(chart_delivery=dict(front_source_sequence=10 if same else 9, front_source_epoch=2),
        fields={"quality.internet_reachable": dict(value=screen)}))
    r = checker.analyse(root, now=NOW)
    assert any(f["section"] == "Screen consistency" and f["level"] == "Problem" for f in r["findings"]) == problem
    checker.inspect_events(r, run.name, [dict(kind="core_internet_timeline_change", data=dict(current=outage))])
    assert r["diagnostics"]["event_counts"][run.name]["net_down"] == 1
    assert any("1 Internet disconnect records" in s for s in r["sections"][checker.SECTIONS[2]])


def test_checker_cannot_reintroduce_invented_down_disposition():
    tree = ast.parse(Path(checker.__file__).read_text())
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "unreachable" not in constants


def test_selected_forecast_not_replaced_by_an_alternative_score():
    d = decision()
    d["payload"]["forecast"].update(opportunity_id="learned_lte:PBAND=B3|CA=B1+B3+B20", expected_aq=2.3, evidence_cell_id=2050, evidence_pci=0)
    r = checker.blank_report(NOW)
    checker.make_decisions([d], [], r)
    text = r["decisions"][0]["selected"]
    assert "Band B3" in text and "+2.3" in text and "Cell 2050" in text and "PCI 0" in text


def test_command_receipt_keeps_failure_and_active_state(tmp_path):
    root, _ = stage(tmp_path)
    put(root / "portable_runtime/guc_memory/cgf_state/command_authority.json", dict(active_cellular=dict(command_id="unfinished"),
        terminal=[dict(terminal_ts=NOW-120, terminal_status="failed", family="reboot", reason="readback timeout")]))
    r = checker.analyse(root, now=NOW)
    assert any("active command" in f["text"] for f in r["findings"])
    assert any("reboot" in s and "failed" in s for s in r["sections"]["Commands"])


@pytest.mark.parametrize("bad", [None, 5, "wrong"])
def test_malformed_optional_evidence_does_not_hide_other_checks(tmp_path, bad):
    root, run = stage(tmp_path, fault_count=1)
    data = json.loads((run / "new_horizon_diagnostic_snapshot.json").read_text())
    data["adviser_chain"] = dict(status="complete", candidates=bad)
    put(run / "new_horizon_diagnostic_snapshot.json", data)
    put(root / "portable_runtime/guc_memory/cgf_state/command_authority.json", dict(terminal=bad))
    r = checker.analyse(root, now=NOW)
    assert any("runtime faults" in f["text"] for f in r["findings"])
    assert any("malformed" in f["text"] for f in r["findings"])


def test_compressed_bomb_is_bounded_and_reported(tmp_path, monkeypatch):
    root, run = stage(tmp_path)
    with gzip.open(run / "large.log.gz", "wb") as f:
        f.write(b"x" * 5000)
    monkeypatch.setattr(checker, "MAX_TEXT", 1000)
    r = checker.analyse(root, now=NOW)
    assert any("fully read" in f["text"] for f in r["findings"])


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="Windows clock tested natively; explicit TZ available on POSIX")
def test_dst_repeated_pc_hour_keeps_both_distinct_intervals(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        r = checker.blank_report(datetime(2026,10,25,4,30,tzinfo=timezone.utc).timestamp())
        checker.hourly_rows(r, [], [], [])
        repeated = [h for h in r["hourly"] if "25 Oct 2026 02:00:00" in h["interval"].split(" to ")[0]]
        assert len(repeated) == 2
        assert repeated[0]["start"] != repeated[1]["start"]
        assert all("+0200" not in h["interval"] and "+0100" not in h["interval"] for h in repeated)
        assert sum(h["end"]-h["start"] for h in r["hourly"]) == 86400
    finally:
        monkeypatch.undo()
        time.tzset()
@pytest.mark.parametrize('disposition', ['stale', 'fresh', 'missing', 'foreign', 'rejected'])
def test_configured_pool_readback_age_is_checked_without_guessing_router_outcome(tmp_path, disposition):
    """The October14 escape must be found even when all processes are healthy."""
    from arch4.core_contracts.command_targets import CoreBandSetTarget
    from arch4.core_contracts.physical_commands import PhysicalCommandDispatchEvidence
    scope=dict(source_epoch='scan',session_scope_key='backend=b818|serial=SIMULATED',lifecycle_generation=1)
    at=NOW-30.
    stage=PhysicalCommandDispatchEvidence(stage_kind='band_selection',ordinal=1,
        request_dispatched_at=at+10.,transport_returned_at=at+12.,
        outcome='rejected' if disposition=='rejected' else 'response_unavailable',
        reason='native-response',detail={},target=CoreBandSetTarget((1,3)))
    row=dict(terminal_ts=at+15.,terminal_status='timed_out',reason='not_confirmed',
             matrix_identity={**scope,'source_epoch':'other' if disposition=='foreign' else 'scan'},
             receipts={'modem':{'stages':[stage.as_dict()]}})
    cell=dict(state='missing' if disposition=='missing' else 'available',
              acquired_at=at+14. if disposition=='fresh' else at,value=[1,3,20,28])
    matrix={**scope,'lines':{'radio':{'cells':{'configured_band_pool':cell}}}}
    authority=tmp_path/'guc_memory/cgf_state';authority.mkdir(parents=True)
    output=tmp_path/'guc_output';output.mkdir()
    (authority/'command_authority.json').write_text(json.dumps({'terminal':[row],'active_cellular':None}),encoding='utf-8')
    (output/'current_state.json').write_text(json.dumps({'core_snapshot':{'registers':{'modem_live_matrix':{'value':matrix}}}}),encoding='utf-8')
    r=checker.blank_report(NOW);r['since']=at-1.
    checker.inspect_commands(r,tmp_path)
    matches=[f for f in r['findings'] if 'pool predates' in f['text']]
    assert bool(matches) is (disposition=='stale')


@pytest.mark.parametrize('snapshot_offset', [0., 11., 14., 15., 20.])
def test_pool_check_respects_device_snapshot_time_against_later_action_result(tmp_path, snapshot_offset):
    from arch4.core_contracts.command_targets import CoreBandSetTarget
    from arch4.core_contracts.physical_commands import PhysicalCommandDispatchEvidence
    at = NOW - 30.
    scope = dict(source_epoch='scan', session_scope_key='backend=h173|serial=SYNTHETIC', lifecycle_generation=1)
    stage = PhysicalCommandDispatchEvidence(stage_kind='band_selection', ordinal=1,
        request_dispatched_at=at+10., transport_returned_at=at+12.,
        outcome='response_unavailable', reason='native-response', detail={},
        target=CoreBandSetTarget((1, 3)))
    row = dict(terminal_ts=at+15., terminal_status='timed_out', reason='not_confirmed',
        matrix_identity=scope, receipts={'modem': {'stages': [stage.as_dict()]}})
    matrix = {**scope, 'lines': {'radio': {'cells': {
        'configured_band_pool': dict(state='available', acquired_at=at, value=[1, 3, 7, 20])}}}}
    authority = tmp_path/'guc_memory/cgf_state'; authority.mkdir(parents=True)
    (authority/'command_authority.json').write_text(json.dumps({'terminal': [row], 'active_cellular': None}), encoding='utf-8')
    current = dict(published_at=at+30., core_snapshot=dict(published_at=at+snapshot_offset,
        registers={'modem_live_matrix': {'value': matrix}}))
    result = checker.blank_report(NOW); result['since'] = at-1.
    checker.inspect_commands(result, tmp_path, current_state=current)
    codes = {item.get('code') for item in result['findings']}
    assert ('pool_readback_stale' in codes) is (snapshot_offset >= 15.)
    assert ('pool_snapshot_older_than_result' in codes) is (snapshot_offset < 15.)
    assert all(item['level'] == 'Missing evidence' for item in result['findings']
               if item.get('code') == 'pool_snapshot_older_than_result')


@pytest.mark.parametrize('damage', ['truncated', 'crc'])
def test_damaged_gzip_keeps_other_findings_and_writes_report(tmp_path, damage):
    root, run = stage(tmp_path, fault_count=1)
    body = bytearray(gzip.compress(b'ordinary saved text\n'))
    if damage == 'truncated':
        body = body[:-5]
    else:
        body[-8] ^= 1
    path = run / 'damaged.log.gz'
    path.write_bytes(body)
    result = checker.analyse(root, now=NOW)
    assert any('fully read' in f['text'] for f in result['findings'])
    assert any('runtime faults' in f['text'] for f in result['findings'])
    checker.write_report(result, tmp_path / 'review')
    assert len(list((tmp_path / 'review').glob('report.*'))) == 3
    assert path.read_bytes() == body


@pytest.mark.parametrize('body', [
    b'{"ts": NaN}', b'{"ts": 1e999}',
    ('{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}').encode(),
    ('{"ts":' + '9' * 400 + '}').encode(),
])
def test_invalid_json_numbers_and_nesting_are_visible_not_crashes(tmp_path, body):
    root, run = stage(tmp_path, fault_count=1)
    target = run / 'new_horizon_diagnostic_snapshot.json'
    target.write_bytes(body)
    result = checker.analyse(root, now=NOW)
    checker.write_report(result, tmp_path / 'review')
    assert any(f['source'] == target.name for f in result['findings'])
    assert any('runtime faults' in f['text'] for f in result['findings'])
    assert target.read_bytes() == body


@pytest.mark.parametrize('bad', [10 ** 400, 1e300, float('inf'), float('nan'), [], None])
def test_invalid_command_timestamp_is_reported_and_does_not_crash(tmp_path, bad):
    root, _ = stage(tmp_path)
    put(root / 'portable_runtime/guc_memory/cgf_state/command_authority.json',
        dict(terminal=[dict(terminal_ts=bad, terminal_status='settled')]))
    result = checker.analyse(root, now=NOW)
    assert any(f['source'] == 'command_authority.json' for f in result['findings'])
    checker.write_report(result, tmp_path / 'review')


@pytest.mark.parametrize('expected_aq', [None, 2.4])
def test_normalized_forecast_is_never_mislabelled_aq(expected_aq):
    result = checker.blank_report(NOW)
    cb = dict(expected_service_value=.75)
    if expected_aq is not None:
        cb['expected_aq'] = expected_aq
    checker.hourly_rows(result, [], [], [dict(captured_at=NOW-1,
        adviser_chain=dict(status='complete', candidates=[dict(
            opportunity_id='do_nothing', crystal_ball=cb)]))])
    text = json.dumps(result['hourly'])
    assert 'forecast AQ +0.8' not in text
    assert ('forecast AQ +2.4' in text) if expected_aq else ('forecast service value 0.750' in text)


def test_historical_no_action_evaluations_are_in_their_own_hour(tmp_path):
    root, _ = stage(tmp_path)
    at = NOW-7200
    rows = [dict(kind='brain_evaluation_v1', scope_key='physical-one',
                 event_key='evaluation-'+str(i), observed_at=at+i,
                 payload=dict(schema='NewHorizonBrainEvaluation.v1', complete=True,
                     selected_opportunity_id='do_nothing', reason='hysteresis_hold',
                     explanation='No better supported target.', adviser_chain={})) for i in range(2)]
    result = checker.analyse(root, now=NOW, history_path=history(tmp_path, rows))
    row = next(h for h in result['hourly'] if h['start'] <= at < h['end'])
    assert any('2 evaluations' in s and 'hysteresis hold' in s for s in row['selected'])
    assert not result['hourly'][-1]['selected']


def test_report_scrubs_structured_private_data_without_mutating_input(tmp_path):
    result = checker.blank_report(NOW)
    result['summary'] = 'Saved evidence reviewed.'
    result['brain_evaluations'] = [dict(payload=dict(
        password='private-password-value', detail=r'C:\Users\PrivatePerson\data',
        email='private-person@example.net', imei='123456789012345', pci=0))]
    before = json.dumps(result)
    checker.write_report(result, tmp_path / 'review')
    saved = (tmp_path / 'review/report.json').read_text(encoding='utf-8')
    assert all(value not in saved for value in ('private-password-value', 'PrivatePerson',
               'private-person@example.net', '123456789012345'))
    assert json.dumps(result) == before


def test_cli_read_failure_returns_controlled_error(tmp_path, monkeypatch, capsys):
    from tools.log_checker import app
    root, _ = stage(tmp_path)
    monkeypatch.setattr(app, 'run_check', lambda *args: (_ for _ in ()).throw(
        OSError('private-person@example.net')))
    assert app.main(['--app-dir', str(root), '--check']) == 2
    assert 'private-person' not in capsys.readouterr().err


@pytest.mark.parametrize('case', ['exact', 'foreign', 'wrong_id', 'wrong_sequence', 'tampered'])
def test_canonical_command_completion_joins_only_exact_brain_request(tmp_path, case):
    from arch4.tests.test_command_transactions import _action
    from arch4.command_transactions import (CommandTerminalReceipt, CommandTerminalStatus,
                                          build_cellular_transaction)
    action = _action(command_id='p1', session_scope_key='router-one')
    transaction = build_cellular_transaction(action, now=NOW-2000)
    receipt = CommandTerminalReceipt(receipt_sequence=1, transaction=transaction,
        terminal_status=CommandTerminalStatus.FAILED, reason='native_rejection',
        terminal_ts=NOW-1500).as_dict()
    if case == 'tampered':
        receipt['reason'] = 'changed without checksum'
    root, run = stage(tmp_path)
    runtime = root / 'portable_runtime'
    put(runtime / 'guc_memory/cgf_state/command_authority.json', dict(terminal=[receipt]))
    put(run / 'current_state.json', dict(core_snapshot=dict(matrix_identity=dict(
        session_scope_key='foreign' if case == 'foreign' else 'router-one'))))
    d = decision(at=NOW-2100, pid='wrong' if case == 'wrong_id' else 'p1')
    d['payload']['decision_frame']['matrix_sequence'] = 2 if case == 'wrong_sequence' else 1
    result = checker.analyse(root, now=NOW, history_path=history(tmp_path, [d]))
    missing = any('no retained terminal' in f['text'] for f in result['findings'])
    assert missing is (case != 'exact')
    if case == 'exact':
        assert 'native rejection' in result['decisions'][0]['result']
        assert 'AQ' not in result['decisions'][0]['result']


def test_analysis_pool_check_uses_timestamped_selected_run(tmp_path):
    root, run = stage(tmp_path)
    scope = dict(source_epoch='epoch-one', session_scope_key='router-one', lifecycle_generation=1)
    matrix = {**scope, 'lines': {'radio': {'cells': {'configured_band_pool':
        dict(state='available', acquired_at=NOW-40, value=[1, 3, 20])}}}}
    put(run / 'current_state.json', dict(core_snapshot=dict(
        matrix_identity={**scope, 'matrix_sequence': 5},
        registers={'modem_live_matrix': {'value': matrix}})))
    put(root / 'portable_runtime/guc_memory/cgf_state/command_authority.json', dict(terminal=[dict(
        terminal_ts=NOW-10, terminal_status='timed_out', matrix_identity=scope,
        receipts={'modem': {'stages': [dict(stage_kind='band_selection',
             outcome='response_unavailable', request_dispatched_at=NOW-20)]}})]))
    result = checker.analyse(root, now=NOW)
    assert any('pool predates' in f['text'] for f in result['findings'])


@pytest.mark.parametrize('bad', [None, 1, {'unexpected': True}])
def test_malformed_decision_options_keep_valid_sibling_and_report(tmp_path, bad):
    root, _ = stage(tmp_path)
    first, second = decision(pid='broken'), decision(pid='valid', at=NOW-200)
    first['payload']['decision_frame']['candidate_ids'] = bad
    result = checker.analyse(root, now=NOW, history_path=history(tmp_path, [first, second]))
    assert len(result['decisions']) == 2
    assert result['decisions'][1]['options']
    assert any('options' in f['text'] and f['level'] == 'Missing evidence' for f in result['findings'])
    checker.write_report(result, tmp_path / 'review')


def test_bad_fault_checkpoint_does_not_hide_valid_history(tmp_path):
    root, _ = stage(tmp_path)
    path = history(tmp_path, [decision()])
    owner = AdvisoryHistory(path)
    owner.save_checkpoint(name='runtime_faults_v2', scope_key='new_horizon_runtime',
                           updated_at=NOW, payload=dict(schema='NewHorizonRuntimeFaults.v2', faults=None))
    owner.close()
    result = checker.analyse(root, now=NOW, history_path=path)
    assert len(result['decisions']) == 1
    assert any('fault history' in f['text'] for f in result['findings'])


def test_offline_import_does_not_load_live_engines_or_modem_catalog():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c',
        "import sys; from tools.log_checker import report; "
        "assert not ({'arch4.new_horizon.advisory','arch4.new_horizon.runtime',"
        "'arch4.modem.catalog','arch4.modem_live.connection_profiles',"
        "'arch4.operator_profiles'} & sys.modules.keys())"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
