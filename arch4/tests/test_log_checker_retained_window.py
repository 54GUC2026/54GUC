"""Stopped folders are reviewed at their saved time, without borrowing History."""
import json
import hashlib

import pytest

from tools.log_checker import report as checker
from tools.log_checker.explanations import FINDINGS
from arch4.tests.test_offline_log_checker import NOW, decision, put, stage
from arch4.tests.test_offline_observation_coverage import history


REVIEWED_AT = NOW + 3 * 86400


def test_stopped_folder_uses_saved_day_and_preserves_exact_history_context(tmp_path):
    root, run = stage(tmp_path)
    rows = [decision(), decision(NOW + 3600, "future-same-context"),
            dict(kind="observation", scope_key="radio-context-one", event_key="old-measurement",
                 observed_at=NOW-30, payload=dict(continuity_key="physical-one", complete=True)),
            dict(kind="observation", scope_key="radio-context-other", event_key="foreign-measurement",
                 observed_at=NOW-20, payload=dict(continuity_key="another-sim", complete=True))]
    foreign = decision(NOW-200, "foreign-proposal")
    foreign["scope_key"] = "another-sim"
    rows.append(foreign)
    path = history(tmp_path, rows)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = checker.analyse(root, now=REVIEWED_AT, history_path=path)
    assert result["generated_at"] == REVIEWED_AT
    assert result["until"] == NOW
    assert result["since"] == NOW-86400
    assert result["runs"] == [run.name]
    assert len(result["decisions"]) == 1
    codes = {entry["code"] for entry in result["findings"]}
    assert "history_context_missing" not in codes
    assert "observations_missing" not in codes
    integrity = " ".join(result["sections"][checker.SECTIONS[6]])
    assert "1 decoded same-context observations" in integrity
    assert "other recorded SIM/network contexts" in integrity
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("snapshot,field", [
    ("runtime_health", "ts"), ("current_state", "published_at"),
    ("new_horizon_diagnostic_snapshot", "captured_at"),
    ("ngui_page_truth", "published_at"), ("shutdown_receipt", "finished_at"),
])
def test_window_ends_at_latest_saved_app_record_not_only_health(tmp_path, snapshot, field):
    root, run = stage(tmp_path)
    path = run / (snapshot + ".json")
    payload = json.loads(path.read_text()) if path.exists() else {}
    payload[field] = NOW+90
    put(path, payload)
    result = checker.analyse(root, now=REVIEWED_AT)
    assert result["until"] == NOW+90
    assert result["generated_at"] == REVIEWED_AT
    assert result["window_basis"] == "latest_retained_app_record"


def test_future_dated_run_is_excluded_without_changing_window_or_identity(tmp_path):
    root, run = stage(tmp_path)
    future = root / "portable_runtime" / "guc_output_future"
    put(future / "runtime_health.json", dict(ts=REVIEWED_AT+100, faults=dict(fault_count=9)))
    put(future / "current_state.json", dict(published_at=REVIEWED_AT+100))
    put(future / "new_horizon_diagnostic_snapshot.json", dict(captured_at=REVIEWED_AT+100,
        referee_assessment_history=dict(continuity_key="another-sim")))
    result = checker.analyse(root, now=REVIEWED_AT)
    assert result["until"] == NOW
    assert result["runs"] == [run.name]
    assert any(entry["code"] == "run_time_invalid" for entry in result["findings"])
    assert not any(entry["code"] == "runtime_faults" for entry in result["findings"])


def test_no_saved_app_time_keeps_review_clock_explicit(tmp_path):
    root, run = stage(tmp_path)
    put(run / "runtime_health.json", dict(faults=dict(fault_count=0)))
    diagnostic = json.loads((run / "new_horizon_diagnostic_snapshot.json").read_text())
    diagnostic.pop("captured_at")
    put(run / "new_horizon_diagnostic_snapshot.json", diagnostic)
    result = checker.analyse(root, now=REVIEWED_AT)
    assert result["until"] == REVIEWED_AT
    assert result["window_basis"] == "review_time_no_saved_app_time"


def test_empty_matching_window_does_not_accuse_user_of_wrong_router():
    title, meaning, impact, next_step = FINDINGS["history_context_missing"]
    assert "review window" in meaning.lower()
    assert "does not establish" in meaning.lower()
    assert "does not match" not in meaning.lower()
    assert "do not reset" in next_step.lower()


def test_report_labels_saved_window_separately_from_creation_time(tmp_path):
    root, _ = stage(tmp_path)
    result = checker.analyse(root, now=REVIEWED_AT)
    checker.write_report(result, tmp_path / "review")
    for suffix in ("txt", "html"):
        body = (tmp_path / "review" / ("report."+suffix)).read_text(encoding="utf-8")
        assert "last saved app record" in body.lower()
        assert checker.clock(NOW) in body
        assert checker.clock(REVIEWED_AT) in body
