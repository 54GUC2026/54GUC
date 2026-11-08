"""Operator-only replay of a known stopped run; not a customer bypass switch."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from tools.log_checker.report import analyse, snapshot_database, write_report


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replay(root, history, output):
    runs = sorted((root / "portable_runtime").glob("guc_output*"))
    last = 0.0
    checked = []
    for run in runs:
        health_file = run / "runtime_health.json"
        if not health_file.is_file():
            continue
        health = json.loads(health_file.read_text(encoding="utf-8-sig"))
        receipt = json.loads((run / "shutdown_receipt.json").read_text(encoding="utf-8-sig"))
        if receipt.get("status") != "complete" or receipt["finished_at"] < health["ts"]:
            raise ValueError("Replay requires a completed shutdown after the last saved health sample")
        last = max(last, receipt["finished_at"])
        checked.extend(p for p in run.iterdir() if p.is_file())
    if not last:
        raise ValueError("No stopped run")
    checked.extend((root / "portable_runtime/guc_memory/cgf_state").glob("*.json"))
    before = {p: digest(p) for p in checked}
    # A separate read-only backup freezes even a shared store. Only this private
    # copy is used by the review, with a cutoff at the old run's actual shutdown.
    with tempfile.TemporaryDirectory(prefix="checker-replay-") as folder:
        copy = Path(folder) / "history.sqlite3"
        snapshot_database(history, copy)
        history_hash = digest(copy)
        report = analyse(root, now=last, history_path=copy)
        assert digest(copy) == history_hash, "Checker changed its input store"
    assert all(digest(p) == value for p, value in before.items()), "Checker changed stopped-run files"
    assert len(report["runs"]) > 0
    assert abs(sum(r["end"] - r["start"] for r in report["hourly"]) - 86400) < .01
    assert all(report["since"] <= d["at"] <= last for d in report["decisions"])
    write_report(report, output)
    receipt = dict(stopped_files_unchanged=len(before), private_history_unchanged=True,
                   review_cutoff=last, runs=report["runs"], decisions=len(report["decisions"]),
                   hourly_rows=len(report["hourly"]), findings=report["findings"],
                   record_summary=report["sections"]["Record integrity"])
    (output / "replay_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    replay(args.app, args.history, args.output)
