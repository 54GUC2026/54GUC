"""Plain-English projection of recorded events; never operate or regrade CGF."""
from __future__ import annotations

import json


def unique(lines):
    return list(dict.fromkeys(line for line in lines if line))


def connection(identity):
    identity = identity if isinstance(identity, dict) else {}
    band, nr = identity.get("pband"), identity.get("nr_band")
    parts = []
    if type(band) is int:
        parts.append(f"4G band {band}")
    if type(nr) is int:
        parts.append(f"5G band {nr}")
    return " with ".join(parts) or "a connection whose radio details were not saved"


def event_paragraph(event, clock):
    at = clock(event.get("at", 0))
    kind = event.get("kind")
    if kind == "outage":
        return (f"{at}: Internet access was interrupted for {event['duration_s']:.1f} seconds, "
                "then recovered.")
    if kind == "restart_bound":
        return (f"{at}: Saved system uptime places a router restart around this time. "
                "These records cannot distinguish a power cycle from a software reboot or identify who triggered it.")
    if kind == "command":
        action = {"band_selection":"change the allowed radio bands", "pband":"try a different primary 4G band",
                  "radio_mode":"change the network mode", "restart":"restart the router",
                  "mobile_data":"change mobile-data connectivity", "network_settings":"change network settings",
                  "wifi_radio":"change a Wi-Fi radio"}.get(event.get("command"),"change radio settings")
        line = f"{at}: A request to {action} was recorded."
        if event.get("terminal_status") == "settled":
            line += " The router's resulting settings were confirmed"
            if event.get("terminal_at"):
                line += " at " + clock(event["terminal_at"])
            return line + "."
        return line + " The available receipt does not confirm the final settings."
    if kind == "connection":
        periods = event.get("periods", 1)
        prefix = (f"In {periods} separate sampled periods between {at} and {clock(event['until'])}, "
                  if periods > 1 else f"In saved readings from {at} to {clock(event['until'])}, ")
        line = prefix + "the records show " + connection(event.get('identity')) + "."
        scores = event.get("scores", [])
        if scores and max(scores) - min(scores) >= .1:
            line += f" The saved hourly service scores ranged from {min(scores):+.2f} to {max(scores):+.2f}."
        if event.get("pool_changed"):
            pool = event["identity"]["ca_pool"]
            line += " By this period, the allowed 4G bands were " + ", ".join(str(x) for x in pool) + "."
        return line
    if kind == "decision":
        band = event.get("requested_band")
        target = f"try 4G band {band}" if type(band) is int else "try a different connection"
        line = f"{at}: Brain recommended that the router {target}."
        if event.get("submitted") is False:
            return line + " The request was not sent to the router."
        outcomes = [row for row in event.get("outcomes", []) if row.get("final") and row.get("status") == "graded"]
        if not outcomes:
            if event.get("terminal_status") == "settled":
                line += " The router's resulting settings were confirmed, but no completed quality assessment was retained."
                if any(str(reason).startswith('target_did_not_hold:')
                       for row in event.get('outcomes', []) for reason in row.get('reasons', [])):
                    line += " The router moved away from the target before that assessment could finish."
                return line
            return line + " The saved records cannot confirm a completed result or its quality."
        outcome = outcomes[-1]
        line += " The completed assessment recorded " + connection(outcome.get("identity")) + "."
        grade = outcome.get("grade")
        if type(grade) in (int, float):
            line += f" Its recorded service-quality score was {grade:+.2f}."
        if outcome.get("forecast_comparable") is False:
            if (outcome.get("forecast_reason") == "different_serving_cell"
                    or (type(band) is int and type(outcome.get("identity",{}).get("pband")) is int
                        and band != outcome["identity"]["pband"])):
                line += " The actual connection differed from the forecast, so this result cannot verify that forecast."
            else:
                line += " The saved result cannot be paired with the forecast, so the forecast cannot be verified."
        return line
    return ""


def timeline(events):
    """Group repeated sampled configurations, explicitly retaining disjoint periods."""
    seen, result = set(), []
    decision_ids = {row.get("command_id") for row in events if row.get("kind") == "decision" and row.get("command_id")}
    for event in sorted(events, key=lambda row: (row.get("at", 0), row.get("kind", ""))):
        if event.get("kind") == "command" and event.get("command_id") in decision_ids:
            continue
        key = json.dumps(event, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        row = dict(event)
        if row.get("kind") == "connection":
            row["periods"] = 1
            row["scores"] = [row["aq"]] if type(row.get("aq")) in (int,float) else []
        if row.get("kind") == "connection" and result:
            previous = result[-1]
            if (previous.get("kind") == "connection"
                    and previous.get("identity") == row.get("identity")
                    and previous.get("until", -1) <= row.get("until", -1)):
                previous["periods"] += int(previous["until"] < row["at"])
                previous["scores"].extend(row["scores"])
                previous["until"] = row["until"]
                continue
        result.append(row)
    return result


def build_story(report, clock):
    """Return short chapters, retaining detector severity and evidence limits."""
    findings = report.get("findings", [])
    problems = [row for row in findings if row["level"] == "Problem"]
    warnings = [row for row in findings if row["level"] == "Warning"]
    gaps = [row for row in findings if row["level"] == "Missing evidence" and row["code"] != "field_values_missing"]
    views = report.get("field_availability", {}).values()
    fields = [field for view in views for field in view.get("fields", [])]
    missing = [row for row in fields if row["state"] in {"missing", "invalid"}]
    invalid = [row for row in missing if row["state"] == "invalid"]
    expected = [row for row in missing if row.get("reason") == "modem_live_profile_supported_not_observed"]
    optional_inventory = any(row["code"] == "field_values_missing" for row in findings)
    snapshots = list(report.get("story_snapshots", {}).values())
    live_export = bool(snapshots) and all(row.get('live_export') is True for row in snapshots)
    if live_export:
        gaps = [row for row in gaps if row['code'] != 'shutdown_missing']
    headline = ("The records contain a confirmed problem that needs attention." if problems else
                "Some recorded events need your attention." if warnings else
                "No confirmed software fault was found in the checks that could run.")
    opening = [headline]
    if str(report.get("summary", "")).startswith("Synthetic package test"):
        opening = [report["summary"]]
    if snapshots and all(row.get("shutdown_complete") is True for row in snapshots):
        opening.append("The saved app runs ended with a completed shutdown.")
    elif live_export:
        opening.append("This support bundle was captured while 54GUC was running. Its later shutdown is outside the capture.")
    if report.get("device_mode") in {"4G", "5G"}:
        mode = report["device_mode"]
        opening.append(f"The recorded router is a {mode}-capable device; its actual connections are described below.")
    events = timeline(report.get("story_events", []))
    connections = [row for row in events if row.get('kind') == 'connection']
    path = []
    for row in connections:
        description = connection(row.get('identity'))
        if not path or path[-1] != description:
            path.append(description)
    if path:
        identities = [row.get('identity', {}) for row in connections]
        nr_bands = {identity.get('nr_band') for identity in identities}
        if len(nr_bands) == 1 and all(type(identity.get('pband')) is int for identity in identities):
            bands = []
            for identity in identities:
                if not bands or bands[-1] != identity['pband']:
                    bands.append(identity['pband'])
            description = '4G band ' + ', then '.join(str(band) for band in bands)
            nr = next(iter(nr_bands))
            if type(nr) is int:
                description += f', with 5G band {nr} in the saved readings'
        else:
            description = ', then '.join(path)
        opening.append('Saved radio path: ' + description + '.')
        periods = sum(row.get('periods', 1) for row in connections)
        coverage = f'{periods} separate sampled periods' if periods > 1 else 'sampled readings'
        opening.append('This describes ' + coverage + ', not proof of continuous Internet access.')
        scores = [score for row in connections for score in row.get('scores', [])]
        if scores and max(scores) - min(scores) >= .1:
            opening.append(f'Saved hourly service scores ranged from {min(scores):+.2f} to {max(scores):+.2f}; higher is better.')
    passages = unique(event_paragraph(event, clock) for event in events if event.get('kind') != 'connection')
    if not passages:
        passages = ["The available records do not provide a detailed sequence of connection changes. "
                    "This does not mean the connection stayed unchanged."]
    if report.get("decisions") and not any(row.get("kind") == "decision" for row in events):
        passages.append("Saved recommendations are listed in the annex. This older report did not retain enough structured detail to retell their outcomes safely.")
    attention = []
    for row in problems + warnings:
        attention.append(row["title"] + ". " + row["meaning"])
    if invalid:
        attention.append("Some saved readings were invalid. That can prevent checks that depend on them; the annex identifies the affected readings and recorded reasons.")
    if expected:
        attention.append("Some supported fields had no recorded value. The annex separates them from unsupported features; absence alone does not identify the cause.")
    if optional_inventory and not invalid and not expected:
        attention.append("Some additional router details were unavailable; see the annex. Unsupported fields are excluded from missing-data alarms.")
    limits = ["Internet interruptions cannot be ruled out outside the retained event records. Their causes are unknown unless stated above."]
    if gaps:
        limits.extend(unique(row["title"] + ". " + row["meaning"] for row in gaps))
    actions = unique(row["next_step"] for row in problems + warnings)
    return [(heading, paragraphs) for heading, paragraphs in [
        ("Your connection", unique(opening)),
        ("What happened", passages),
        ("What needs attention", unique(attention)),
        ("What could not be checked", unique(limits)),
        ("What to do", actions),
    ] if paragraphs]
