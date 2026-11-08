"""History population is independent of whether a learning context has closed."""
import hashlib
import ast
from pathlib import Path

import pytest

from arch4.new_horizon.history import AdvisoryHistory, _compact_observations
from arch4.new_horizon.hourly_aq import make_aq_block, merge_aq_block
from arch4.tests.test_hourly_aq_contract import identity
from arch4.tests.test_offline_log_checker import NOW, put, stage
from tools.log_checker import report as checker


def history(tmp_path, rows):
    path = tmp_path / "history.sqlite3"
    owner = AdvisoryHistory(path)
    try:
        for row in rows:
            args = {k: v for k, v in row.items() if k != "kind"}
            if row["kind"] == "observation":
                owner.admit_observation(**args)
            else:
                owner.record(kind=row["kind"], **args)
    finally:
        owner.close()
    return path


def observation(scope, sequence, *, code="current", continuity="physical-one", at=None, complete=True):
    return dict(kind="observation", scope_key=scope, event_key=f"{scope}:{sequence}",
                observed_at=NOW-100+sequence if at is None else at,
                payload=dict(identity_code=code, continuity_key=continuity,
                             matrix_sequence=sequence, complete=complete))


def bind_run(run, *, code="current", epoch="epoch-one", command_epoch=None):
    matrix = dict(session_scope_key="serial=ROUTER-A", source_epoch=epoch,
                  lifecycle_generation=1, matrix_sequence=5, matrix_revision="a"*64)
    put(run / "current_state.json", dict(core_snapshot=dict(matrix_identity=matrix,
        command_context=dict(identity_code=code, matrix_identity={**matrix,
            "source_epoch": command_epoch or epoch}))))


def count(report):
    notes = report["sections"][checker.SECTIONS[6]]
    return next((int(n.split()[1]) for n in notes if n.startswith("History:")), 0)


def test_open_transient_and_tail_included_without_closed_proof(tmp_path):
    root, run = stage(tmp_path)
    bind_run(run)
    rows = [observation("open", 1), observation("transient", 2, complete=False),
            observation("tail", 3), observation("tail", 4)]
    path = history(tmp_path, rows)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = checker.analyse(root, now=NOW, history_path=path)
    assert count(report) == 4
    assert any("1 observations explicitly" in s for s in report["sections"][checker.SECTIONS[1]])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("continuity", ["", None, ["physical-one"]])
def test_unqualified_rows_are_excluded_and_coverage_is_explicit(tmp_path, continuity):
    root, run = stage(tmp_path)
    bind_run(run)
    path = history(tmp_path, [observation("open", 1), observation("other", 2, continuity=continuity)])
    report = checker.analyse(root, now=NOW, history_path=path)
    assert count(report) == 1
    assert any("1 observations lack a matching retained SIM/network context" in f["text"]
               for f in report["findings"])


@pytest.mark.parametrize("command_epoch", ["epoch-one", "stale-epoch"])
def test_identity_code_cannot_authorize_missing_continuity(tmp_path, command_epoch):
    root, run = stage(tmp_path)
    bind_run(run, command_epoch=command_epoch)
    report = checker.analyse(root, now=NOW, history_path=history(tmp_path,
        [observation("open", 1, continuity=None)]))
    assert count(report) == 0


def test_foreign_retained_run_cannot_authorize_its_observations(tmp_path):
    root, run = stage(tmp_path)
    bind_run(run)
    other = run.parent / "guc_output_other"
    put(other / "runtime_health.json", dict(ts=NOW-10))
    put(other / "new_horizon_diagnostic_snapshot.json",
        dict(referee_assessment_history=dict(continuity_key="another-router")))
    bind_run(other, code="foreign")
    report = checker.analyse(root, now=NOW, history_path=history(tmp_path,
        [observation("open", 1), observation("foreign", 2, continuity="another-router")]))
    assert count(report) == 1
    assert any("1 observations belong to other recorded SIM/network contexts" in s
               for s in report["sections"][checker.SECTIONS[6]])
    assert not any("observations lack" in f["text"] for f in report["findings"])


def test_historical_hash_authorized_by_canonical_hour_is_preserved(tmp_path):
    root, run = stage(tmp_path)
    bind_run(run)
    old_scope = "exact:2050"
    path = history(tmp_path, [observation(old_scope, 1, code="old-hash", continuity=None), observation("open", 2)])
    block = make_aq_block(assessment_id="old", continuity_key="physical-one",
        identity=identity(), opened_at=NOW-1800, ended_at=NOW-1200, aq=1,
        observed_vector={"eci_points": 30})
    owner = AdvisoryHistory(path)
    owner.save_checkpoint(scope_key="physical-one", name=checker.AQ_HOURLY_CHECKPOINT,
        updated_at=NOW-1200, payload=dict(schema=checker.AQ_HOURLY_CHECKPOINT_SCHEMA,
            hours=[merge_aq_block(None, block, timezone="UTC")]))
    owner.close()
    assert count(checker.analyse(root, now=NOW, history_path=path)) == 2


def test_decoded_batch_filters_both_window_edges(tmp_path):
    root, run = stage(tmp_path)
    bind_run(run)
    rows = [observation("open", i, at=at) for i, at in enumerate(
        [NOW-86401, NOW-86400, NOW-10, NOW, NOW+1], 1)]
    batch = dict(kind="observation_batch", scope_key="open", event_key="batch",
                 observed_at=NOW+1, payload=_compact_observations("open", tuple(rows)))
    path = history(tmp_path, [batch])
    assert count(checker.analyse(root, now=NOW, history_path=path)) == 3


def test_export_limit_is_reported_not_silent(tmp_path, monkeypatch):
    root, run = stage(tmp_path)
    bind_run(run)
    path = history(tmp_path, [observation("open", i) for i in range(1, 5)])
    monkeypatch.setattr(checker, "MAX_ROWS", 3)
    report = checker.analyse(root, now=NOW, history_path=path)
    assert count(report) == 3
    assert any("observation" in f["text"] and "limit" in f["text"] for f in report["findings"])


def test_valid_other_scope_survives_corrupt_batch(tmp_path):
    root, run = stage(tmp_path)
    bind_run(run)
    bad = dict(kind="observation_batch", scope_key="bad", event_key="bad",
               observed_at=NOW-10, payload={"schema": "unknown"})
    report = checker.analyse(root, now=NOW, history_path=history(tmp_path, [observation("open", 1), bad]))
    assert count(report) == 1
    assert any("observation scope" in f["text"] for f in report["findings"])


def test_every_checker_history_call_passes_canonical_continuity():
    tree = ast.parse(Path(checker.__file__).read_text())
    callers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "read_history"]
    assert callers
    assert all(len(n.args) == 3 and isinstance(n.args[1], ast.Name)
               and n.args[1].id == "continuity" for n in callers)
    assert not any(isinstance(n, ast.Name) and n.id == "run_identity_codes"
                   for n in ast.walk(tree))
    # Decoder/writer internals cannot become another offline History owner.
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "arch4.new_horizon.history"
                   and any(a.name.startswith("_") or a.name == "AdvisoryHistory" for a in n.names)
                   for n in ast.walk(tree))
