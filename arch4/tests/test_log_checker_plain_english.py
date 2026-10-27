"""Customer explanations must cover the real detector and survive export."""
import ast
import html
import json
from pathlib import Path

import pytest

from tools.log_checker import report


def test_every_detector_has_a_named_plain_english_explanation(tmp_path):
    from tools.log_checker.explanations import FINDINGS
    tree = ast.parse(Path(report.__file__).read_text(encoding="utf-8-sig"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "finding"]
    assert len(calls) >= 50
    for call in calls:
        keys = {k.arg: k.value for k in call.keywords}
        assert "code" in keys, f"Unexplained detector at line {call.lineno}"
        code = ast.literal_eval(keys["code"])
        assert code in FINDINGS
        r = report.blank_report(1_792_700_000)
        report.finding(r, "Warning", report.SECTIONS[0],
                       "Recorded detail <script>password=secret-test</script>", "input.json", code=code)
        f = r["findings"][0]
        assert all(f.get(k) for k in ("title", "meaning", "impact", "next_step", "code"))
        assert f["meaning"] != f["text"]
        r["summary"] = "Saved evidence reviewed."
        out = tmp_path / code
        report.write_report(r, out)
        for suffix in ("txt", "html"):
            text = (out / ("report." + suffix)).read_text(encoding="utf-8")
            assert "What happened" in text and "Why it matters" in text and "What to do" in text
            assert f["title"] in html.unescape(text)
            assert "secret-test" not in text
        assert "<script>" not in (out / "report.html").read_text(encoding="utf-8")
        saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert saved["findings"][0]["code"] == code


def test_all_profile_review_results_have_useful_explanations():
    from tools.log_checker.explanations import FIELD_RESULTS, field_explanation
    from tools.log_checker import profile_fields
    tree = ast.parse(Path(profile_fields.__file__).read_text(encoding="utf-8-sig"))
    statuses = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and isinstance(n.value, str) and n.value in {
                    "Unsupported", "No native rule", "No saved conversion", "Native read failed",
                    "Context not retained", "Native reply not retained", "Private value withheld",
                    "Private input withheld", "Conversion failed", "Replay matches", "Replay differs",
                    "Unavailable", "Candidate changes value", "Native request not exercised",
                    "Missing reply", "Decoded"}}
    assert statuses <= FIELD_RESULTS.keys()
    for status in statuses:
        e = field_explanation(status)
        assert e["meaning"] and e["next_step"]
    assert "privacy" in field_explanation("Private input withheld")["meaning"].lower()
    assert "repeat" in field_explanation("Replay matches")["meaning"].lower()


def test_unknown_future_result_is_honest_and_not_a_guessed_diagnosis():
    from tools.log_checker.explanations import field_explanation, command_explanation
    assert "not recognised" in field_explanation("future_value")["meaning"]
    assert "not recognised" in command_explanation("future_value")


def test_recorded_failure_is_not_presented_as_a_router_diagnosis(tmp_path):
    r = report.blank_report(1_792_700_000)
    report.finding(r, "Warning", report.SECTIONS[0], "TimeoutError", "journal", code="diagnostic_error")
    r["summary"] = "Review complete."
    report.write_report(r, tmp_path / "review")
    body = (tmp_path / "review/report.txt").read_text(encoding="utf-8")
    assert "cause" in body.lower()
    assert "restart the router" not in body.lower()
    assert r["findings"][0]["level"] == "Warning"


def test_nr_auto_field_readback_and_unconfirmed_command_stay_distinct(tmp_path):
    from tools.log_checker.explanations import command_explanation
    assert "not confirmed" in command_explanation("unconfirmed").lower()
    assert "accepted" in command_explanation("settled").lower()
    assert "quality" in command_explanation("settled").lower()
    from arch4.command_transactions import CommandTerminalStatus
    for status in CommandTerminalStatus:
        assert "not recognised" not in command_explanation(status.value)


def test_report_explains_fields_that_have_no_rule_instead_of_hiding_them(tmp_path):
    r = report.blank_report(1_792_700_000)
    r["summary"] = "Saved evidence reviewed."
    r["profile_fields"] = {"fields": [dict(id=1, field="radio.nr_band", status="Unsupported",
        rule=None, rule_pointer="/fields/radio.nr_band/read", unit=None)], "limits": []}
    report.write_report(r, tmp_path / "review")
    for suffix in ("html", "txt"):
        body = (tmp_path / "review" / ("report." + suffix)).read_text(encoding="utf-8")
        assert "radio.nr_band" in body and "Unsupported" in body
        assert "What to do" in body


def test_final_version_and_useful_normal_summary():
    assert report.VERSION == "3.0"
    r = report.blank_report(1_792_700_000)
    assert not any("guarantee" in line.lower() for line in r["limitations"])


def test_every_saved_internet_recovery_phase_is_explained_without_inventing_a_write():
    from tools.log_checker.explanations import recovery_explanation
    import arch4.foundation.modem_watchdog as watchdog
    tree = ast.parse(Path(watchdog.__file__).read_text(encoding="utf-8-sig"))
    phases = {n.value.value for n in ast.walk(tree) if isinstance(n, ast.keyword)
              and n.arg == "phase" and isinstance(n.value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Subscript)
            and isinstance(x.slice, ast.Constant) and x.slice.value == "phase" for x in node.targets):
            phases.update(n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for phase in phases | {"armed"}:
        assert "no more specific explanation" not in recovery_explanation(phase), phase
    assert "requested" not in recovery_explanation("settling")
    assert "does not say the write happened" in recovery_explanation("rescue_window")
