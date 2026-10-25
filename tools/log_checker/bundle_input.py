"""Read-only History view over the owner's decoded support stream."""
from collections import Counter
import json

from arch4.new_horizon.history import iter_support_history_export

EVALUATION_SUMMARY_SCHEMA = "SupportBrainEvaluationSummary.v1"
_RETAINED_BYTES = 512 * 1024 * 1024


class BundleHistory:
    def __init__(self, content):
        from tools.log_checker.report import checked_json_value
        self.evidence, self.saved_checkpoints = [], []
        retained_bytes = 0
        for row in iter_support_history_export(content, expand_observations=True):
            if row["record_type"] == "manifest":
                continue
            checked_json_value(row)
            if row.get("kind") == "brain_evaluation_v1":
                payload = row["payload"]
                if (payload.get("schema") != "NewHorizonBrainEvaluation.v1"
                        or not isinstance(payload.get("reason"), str)
                        or not isinstance(payload.get("selected_opportunity_id"), (str, type(None)))):
                    raise ValueError("support_brain_evaluation_invalid")
                # Explicit diagnostic projection, never an incomplete runtime object.
                row = {**row, "payload": {"schema": EVALUATION_SUMMARY_SCHEMA,
                    "reason": payload["reason"],
                    "selected_opportunity_id": payload.get("selected_opportunity_id")}}
            retained_bytes += len(json.dumps(row).encode("utf-8"))
            if retained_bytes > _RETAINED_BYTES:
                raise ValueError("support_history_retained_diagnostics_limit")
            (self.saved_checkpoints if row["record_type"] == "checkpoint"
             else self.evidence).append(row)

    def records(self, *, kind, scope_key, since, limit):
        rows = [r for r in self.evidence if r["kind"] == kind
                and r["scope_key"] == scope_key and r["observed_at"] >= since]
        return sorted(rows, key=lambda r: (r["observed_at"], r["event_key"]))[-limit:]

    def checkpoints(self, *, name):
        return [r for r in self.saved_checkpoints if r["name"] == name]

    def catalog(self, since):
        return [(kind,scope,count) for (kind,scope),count in Counter(
            (r["kind"],r["scope_key"]) for r in self.evidence if r["observed_at"] >= since).items()]
