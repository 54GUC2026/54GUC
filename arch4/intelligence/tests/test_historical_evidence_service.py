"""Detached Intelligence tests; excluded from the active CGF denominator."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import zipfile
from pathlib import Path
from time import monotonic, sleep, time
from types import SimpleNamespace

import pytest

import arch4.intelligence.historical_evidence as continuity_module
from arch4.external_memory_evidence_scope import external_projection_scope
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.customer_contracts.historical_evidence import (
    HistoricalEvidenceChartRequest,
    HistoricalEvidenceChartResult,
    HistoricalEvidenceGeneration,
    HistoricalEvidenceImportRequest,
)
from arch4.external_memory_library import build_external_memory_manifest
from arch4.intelligence.historical_evidence import (
    CONSUMERS,
    HistoricalEvidenceRequestSuperseded,
    IntelligenceHistoricalEvidence,
)
from arch4.intelligence.historical_evidence_supervisor import (
    IntelligenceHistoricalEvidenceSupervisor,
)
from arch4.historical_continuity_materialization import (
    materialize_historical_chart_artifact,
)
from arch4.historical_artifact_reader import FAMILY_PRODUCTION
from arch4.historical_continuity_process import (
    HistoricalContinuityBulkRequest,
    HistoricalContinuityChildFault,
    MAX_CHILD_TRACEBACK_CHARS,
    SpawnedHistoricalContinuityBulkProcess,
)
from arch4.historical_continuity_store import CHART_HISTORY_FILENAME
from arch4.portable_state_import import (
    PortableStateConfirmedIdentityRequired,
    auto_import_previous_portable_state,
    import_next_start_handoff,
    import_portable_state,
)
from arch4.telemetry.history_contract import telemetry_formula_vector
from arch4.telemetry.production_contract import build_production_telemetry_row
from arch4.tests.compatibility_identity_fixture import (
    b818_evidence_scope,
    b818_identity_publication,
)
from arch4.tests.historical_continuity_fixture import commit_prior_workspace
from arch4.tests.fault_test_support import make_test_fault_supervisor
from arch4.tests.core_observation_test_support import core_observation_for_test
from arch4.worker_health import WorkerHealthAuthority


def _supervised_engine(engine: SimpleNamespace | None = None) -> SimpleNamespace:
    engine = engine or SimpleNamespace()
    controller = getattr(engine, "controller", None) or SimpleNamespace()
    runtime = getattr(controller, "runtime", None) or SimpleNamespace()
    runtime.fault_supervisor = make_test_fault_supervisor()
    runtime.worker_health = WorkerHealthAuthority()
    controller.runtime = runtime
    engine.controller = controller
    return engine


def _target(
    *,
    now: float | None = None,
    session_scope_key: str = "current-session",
    source_epoch: str = "current-epoch",
) -> dict:
    observed_at = float(time() if now is None else now)
    target = b818_identity_publication(
        {
            "backend": "b818",
            "model": "B818-263",
            "firmware": "11.0.1.1",
            "provider_name": "A1",
            "provider_plmn": "22005",
            "serial": "TEST-B818-SERIAL",
        }
    )
    target["evidence_scope"] = b818_evidence_scope(
        formula_vector=telemetry_formula_vector(),
        provider_plmn="22005",
        session_scope_key=session_scope_key,
        source_epoch=source_epoch,
        source_sequence=101,
        observed_at=observed_at,
    )
    return target


def _request(
    *,
    login_scope: dict | None = None,
    force: bool = False,
    max_age_days: int = 14,
    min_telemetry_rows: int = 100,
) -> HistoricalEvidenceImportRequest:
    return HistoricalEvidenceImportRequest(
        request_id=f"test-history-{time()}",
        created_at=time(),
        confirmed_login_scope=CoreFrozenPayload.from_mapping(login_scope or {}),
        confirmed_handoff_receipt=CoreFrozenPayload.empty(),
        force=force,
        max_age_days=max_age_days,
        min_telemetry_rows=min_telemetry_rows,
    )


def _frame(target: dict) -> CoreObservationFrame:
    scope = target["evidence_scope"]
    return core_observation_for_test(
        session_id=scope["session_scope_key"],
        identity_code=scope["identity_code"],
        identity_status="confirmed",
        access_type=str(target.get("access_type") or "huawei_http_single_lane"),
        source_epoch=scope["source_epoch"],
        source_sequence=int(scope["source_sequence"]),
        sampled_at=float(scope["observed_at"]),
        provider_plmn=str(
            scope.get("provider_plmn") or target.get("provider_plmn") or ""
        ),
        capability_facts={
            **target,
            "catalog_version": scope["identity_catalog_version"],
            "catalog_checksum": scope["identity_catalog_checksum"],
            "provider_plmn": (
                scope.get("provider_plmn") or target.get("provider_plmn")
            ),
            "identity_status": "exact",
        },
    )


class _AcceptedRootPort:
    def __init__(self, frame: CoreObservationFrame | None) -> None:
        self._record = (
            None
            if frame is None
            else SimpleNamespace(
                key=f"accepted-root:{frame.source_sequence}",
                root=SimpleNamespace(observation_frame=frame),
            )
        )
        self._inflight = False
        self._acked = False

    def claim_next(self, _client_id: str):
        if self._record is None or self._inflight or self._acked:
            return None
        self._inflight = True
        return self._record

    def acknowledge(self, _client_id: str, *, key: str) -> None:
        assert self._record is not None and key == self._record.key
        self._inflight = False
        self._acked = True

    def release(self, _client_id: str, *, key: str, error: BaseException) -> None:
        assert self._record is not None and key == self._record.key
        assert isinstance(error, BaseException)
        self._inflight = False

    def status(self) -> dict:
        return {"acked": self._acked, "inflight": self._inflight}


def _service(
    *,
    stage_dir: Path,
    memory_dir: Path,
    executor,
    bulk_execution,
    target: dict | None = None,
) -> IntelligenceHistoricalEvidence:
    accepted_roots = _AcceptedRootPort(
        _frame(target) if target is not None else None
    )
    return IntelligenceHistoricalEvidence(
        stage_dir=stage_dir,
        runtime_dir=stage_dir,
        memory_dir=memory_dir,
        executor=executor,
        bulk_execution=bulk_execution,
        accepted_root_outbox=accepted_roots,
        worker_health=WorkerHealthAuthority(),
        fault_supervisor=make_test_fault_supervisor(),
    )


def _status(service: IntelligenceHistoricalEvidence) -> dict:
    return service.status().detail.to_mapping()


class _RejectingExecutor:
    def submit(self, *_args, **_kwargs):
        raise AssertionError("identity gate must not submit background work")


class _RejectingBulk:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("bulk process must not run")

    def shutdown(self, *, timeout_s: float):
        return {"status": "idle", "timeout_s": timeout_s}


class _PreparedBulk:
    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    def execute(self, _request, progress_callback):
        if self.delay_s:
            sleep(self.delay_s)
        progress_callback(80.0, "source_scan", "production-minimal scan complete")
        return {
            "status": "prepared",
            "auto_import": {"status": "imported"},
            "chart_history": {
                "catalog_published": True,
                "admitted_rows": 250,
            },
            "child_pid": 200,
            "parent_pid": 100,
        }

    def shutdown(self, *, timeout_s: float):
        return {"status": "idle", "timeout_s": timeout_s}


class _FaultingBulk:
    def execute(self, _request, _progress_callback):
        raise HistoricalContinuityChildFault(
            error="OSError: bad descriptor",
            child_traceback="child.py:42 in scan_source\nOSError: bad descriptor",
        )

    def shutdown(self, *, timeout_s: float):
        return {"status": "idle", "timeout_s": timeout_s}


def test_spawned_port_preserves_bounded_child_fault_traceback() -> None:
    traceback_text = "discarded-prefix\n" + ("x" * 13000) + "\nchild-tail"

    class FakeQueue:
        def get(self, *, timeout):
            del timeout
            return {
                "schema": "HistoricalContinuityProcessMessage.v1",
                "kind": "fault",
                "child_pid": 321,
                "error": "OSError: bad descriptor",
                "traceback": traceback_text,
            }

        def close(self):
            return None

        def join_thread(self):
            return None

    class FakeProcess:
        exitcode = 1

        def start(self):
            return None

        def join(self, *, timeout):
            del timeout

        def is_alive(self):
            return False

        def terminate(self):
            raise AssertionError("completed fault process must not be terminated")

    class FakeContext:
        def Queue(self, *, maxsize):
            assert maxsize == 128
            return FakeQueue()

        def Event(self):
            return SimpleNamespace(set=lambda: None)

        def Process(self, **_kwargs):
            return FakeProcess()

    port = SpawnedHistoricalContinuityBulkProcess(process_context=FakeContext())
    request = HistoricalContinuityBulkRequest(
        transaction_id="fault-test",
        attempt=1,
        stage_dir=".",
        staging_generation_dir="./staging",
        target_identity={},
        preferred_sources=(),
        include_default_sources=False,
        force=False,
        max_age_days=14,
        min_telemetry_rows=100,
        confirmed_handoff_receipt={},
    )

    with pytest.raises(HistoricalContinuityChildFault) as caught:
        port.execute(request, lambda *_args: None)

    assert caught.value.error == "OSError: bad descriptor"
    assert len(caught.value.child_traceback) == MAX_CHILD_TRACEBACK_CHARS
    assert caught.value.child_traceback.endswith("child-tail")
    assert "discarded-prefix" not in caught.value.child_traceback


def test_owner_persists_typed_child_fault_evidence(tmp_path: Path) -> None:
    target = _target()
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = _service(
            stage_dir=tmp_path,
            memory_dir=tmp_path / "memory",
            executor=executor,
            bulk_execution=_FaultingBulk(),
            target=target,
        )
        owner.request(_request(login_scope=target["evidence_scope"]))
        deadline = monotonic() + 2.0
        while owner.status().status != "retry_pending":
            assert monotonic() < deadline
            sleep(0.01)
        status = _status(owner)

    assert status["reason"] == (
        "HistoricalContinuityChildFault: OSError: bad descriptor"
    )
    assert status["errors"] == {
        "bulk_execution": "OSError: bad descriptor",
        "child_traceback": "child.py:42 in scan_source\nOSError: bad descriptor",
    }


def test_owner_rejects_bulk_request_without_confirmed_login_scope(tmp_path: Path) -> None:
    target = _target()
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=target,
    )

    with pytest.raises(RuntimeError, match="exact confirmed-login scope"):
        owner.request(_request())


def test_owner_types_overtaken_login_request_without_mutating_owner_state(
    tmp_path: Path,
) -> None:
    target = _target(
        session_scope_key="current-session",
        source_epoch="current-epoch",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=target,
    )
    overtaken = _target(
        session_scope_key="startup-session",
        source_epoch="startup-epoch",
    )["evidence_scope"]
    before = owner.status().to_payload()

    with pytest.raises(HistoricalEvidenceRequestSuperseded) as caught:
        owner.request(_request(login_scope=overtaken))

    assert caught.value.transaction_id == continuity_module._transaction_id(
        external_projection_scope(target, evidence_count=1)
    )
    assert "superseded before Intelligence execution" in caught.value.reason
    assert owner.status().to_payload() == before
    assert not owner.status_path.exists()


def test_previously_admitted_exact_generation_allows_read_only_refresh_request(
    tmp_path: Path,
) -> None:
    target = _target()
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "complete",
                "transaction_id": continuity_module._transaction_id(
                    external_projection_scope(target, evidence_count=1)
                ),
                "confirmed_login_scope": dict(target["evidence_scope"]),
            }
        ),
        encoding="utf-8",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=target,
    )

    status = owner.request(_request())

    assert status.status == "complete"


def test_owner_waits_for_exact_identity_without_submitting(tmp_path: Path) -> None:
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
    )

    status = owner.request(_request(login_scope=_target()["evidence_scope"]))

    assert status.status == "waiting_for_identity"
    assert status.phase == "identity_gate"


def test_owner_returns_queued_before_bulk_work_and_records_all_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    external_scope = external_projection_scope(target, evidence_count=10).as_dict()
    pack = {
        "schema": "external_memory_prior_pack_v1",
        "target_evidence_scope": external_scope,
        "facts": [],
    }
    monkeypatch.setattr(
        continuity_module,
        "commit_staged_generation",
        lambda *_args, **_kwargs: {
            "schema": "HistoricalContinuityCommit.v1",
            "status": "committed",
            "transaction_id": "test",
        },
    )
    monkeypatch.setattr(
        continuity_module,
        "load_external_memory_prior_pack",
        lambda _memory: pack,
    )
    monkeypatch.setattr(
        continuity_module,
        "external_memory_prior_status",
        lambda *_args, **_kwargs: {
            "present": True,
            "activation": {"active": True},
            "fact_count": 2,
            "observation_count": 30,
        },
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = _service(
            stage_dir=tmp_path,
            memory_dir=tmp_path / "memory",
            executor=executor,
            bulk_execution=_PreparedBulk(delay_s=0.08),
            target=target,
        )
        started = monotonic()
        queued = owner.request(_request(login_scope=target["evidence_scope"]))
        elapsed = monotonic() - started
        assert queued.status == "queued"
        assert elapsed < 0.05
        deadline = monotonic() + 3.0
        while owner.status().status not in {"complete", "failed"}:
            assert monotonic() < deadline
            sleep(0.01)
        completed = _status(owner)

    assert completed["status"] == "complete"
    assert completed["progress_pct"] == 100.0
    assert completed["deliveries"] == {consumer: "acked" for consumer in CONSUMERS}
    assert completed["errors"] == {}
    assert completed["intelligence_deliveries"] == {
        consumer: "unattached"
        for consumer in continuity_module.INTELLIGENCE_CONSUMERS
    }
    generation = owner.generation()
    assert isinstance(generation, HistoricalEvidenceGeneration)
    assert generation.transaction_id == completed["transaction_id"]
    assert generation.prior.to_mapping()["present"] is True


def test_presentation_status_is_exact_stable_history_catalog(
    tmp_path: Path,
) -> None:
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
    )

    first = owner.presentation_status()
    second = owner.presentation_status()

    assert first == second
    assert set(first["values"]) == set(
        continuity_module.HISTORICAL_PRESENTATION_SOURCES
    )
    assert first["validity"]["history.predictive_window"] is False
    assert first["reasons"]["history.predictive_window"] == (
        "intelligence_prediction_window_not_published"
    )


def test_service_queues_bounded_chart_read_from_core_identity(
    tmp_path: Path,
) -> None:
    target = _target()
    scope_key = target["evidence_scope"]["session_scope_key"]
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = _service(
            stage_dir=tmp_path,
            memory_dir=tmp_path / "memory",
            executor=executor,
            bulk_execution=_RejectingBulk(),
            target=target,
        )
        request = HistoricalEvidenceChartRequest(
            request_id="chart-empty-proof",
            created_at=time(),
            session_scope_key=scope_key,
        )
        started = monotonic()
        future = owner.request_chart(request)
        assert monotonic() - started < 0.05
        result = future.result(timeout=1.0)

    assert isinstance(result, HistoricalEvidenceChartResult)
    assert result.request_id == request.request_id
    assert result.session_scope_key == scope_key
    assert result.chunks == ()


def test_service_rejects_chart_request_for_noncurrent_scope(tmp_path: Path) -> None:
    target = _target()
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = _service(
            stage_dir=tmp_path,
            memory_dir=tmp_path / "memory",
            executor=executor,
            bulk_execution=_RejectingBulk(),
            target=target,
        )
        with pytest.raises(
            RuntimeError,
            match="historical_evidence_chart_session_scope_mismatch",
        ):
            owner.request_chart(
                HistoricalEvidenceChartRequest(
                    request_id="chart-wrong-scope",
                    created_at=time(),
                    session_scope_key="another-session",
                )
            )


def test_owner_recovers_unfinished_transaction_as_retry_pending(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "running",
                "phase": "source_scan",
                "transaction_id": "continuity-dead-worker",
            }
        ),
        encoding="utf-8",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
    )

    assert owner.status().status == "retry_pending"
    assert owner.status().phase == "crash_recovery"


def test_owner_reports_and_rejects_old_foundation_state_without_crashing_core(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": "HistoricalEvidenceServiceState.v1",
                "owner": "54guc.foundation.historical_evidence",
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )

    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
    )

    status = _status(owner)
    assert status["status"] == "degraded"
    assert status["phase"] == "restore_rejected"
    assert "owner_schema_rejected" in status["reason"]
    health = owner.worker_health.record(continuity_module.RESTORE_WORKER_ID)
    assert health.failure_count == 1
    assert owner.fault_supervisor.status()["fault_count"] == 1


def test_intelligence_history_supervisor_reports_failure_and_recovers(
    tmp_path: Path,
) -> None:
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
    )
    published: list[dict] = []
    attempts = 0

    def publish(status):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected presentation boundary failure")
        published.append(dict(status))

    supervisor = IntelligenceHistoricalEvidenceSupervisor(
        owner,
        worker_health=owner.worker_health,
        fault_supervisor=owner.fault_supervisor,
        publish_status=publish,
        interval_s=0.01,
    )
    supervisor.start()
    deadline = monotonic() + 1.0
    while not published and monotonic() < deadline:
        sleep(0.01)
    receipt = supervisor.shutdown(timeout_s=1.0)

    assert published
    assert receipt["stopped"] is True
    assert receipt["failure_count"] >= 1
    assert receipt["poll_count"] >= 1
    assert owner.fault_supervisor.status()["fault_count"] >= 1


def test_committed_generation_retries_delivery_without_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    transaction_id = continuity_module._transaction_id(
        external_projection_scope(target, evidence_count=1)
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    external_scope = external_projection_scope(target, evidence_count=1).as_dict()
    (memory / "external_memory_promoted_priors.json").write_text(
        json.dumps(
            {
                "schema": "external_memory_prior_pack_v1",
                "target_identity": target,
                "target_evidence_scope": external_scope,
                "summary": {"fact_count": 0, "observation_count": 0},
                "facts": [],
            }
        ),
        encoding="utf-8",
    )
    commit_prior_workspace(
        memory,
        target_identity=target,
        transaction_id=transaction_id,
    )
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "retry_pending",
                "transaction_id": transaction_id,
                "confirmed_login_scope": target["evidence_scope"],
                "next_retry_at_ts": 0.0,
            }
        ),
        encoding="utf-8",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = _service(
            stage_dir=tmp_path,
            memory_dir=memory,
            executor=executor,
            bulk_execution=_RejectingBulk(),
            target=target,
        )
        owner.request(_request(login_scope=target["evidence_scope"]))
        deadline = monotonic() + 2.0
        while owner.status().status != "complete":
            assert monotonic() < deadline, owner.status().to_payload()
            sleep(0.01)

    assert _status(owner)["commit"]["transaction_id"] == transaction_id


def test_service_requeues_admitted_retry_without_scanning_caller_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    scope = external_projection_scope(target, evidence_count=1)
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "retry_pending",
                "phase": "consumer_delivery",
                "transaction_id": continuity_module._transaction_id(scope),
                "confirmed_login_scope": dict(target["evidence_scope"]),
                "next_retry_at_ts": 0.0,
                "preferred_sources": [str(tmp_path / "prior.zip")],
                "max_age_days": 14,
                "min_telemetry_rows": 100,
            }
        ),
        encoding="utf-8",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=target,
    )
    calls: list[HistoricalEvidenceImportRequest] = []

    def record_request(request):
        calls.append(request)
        return owner.status()

    monkeypatch.setattr(owner, "request", record_request)

    owner.service()
    assert calls[0].confirmed_login_scope.to_mapping() == target["evidence_scope"]
    assert calls[0].max_age_days == 14
    assert calls[0].min_telemetry_rows == 100


def test_target_generation_change_discards_old_generation_and_reports_no_source(
    tmp_path: Path,
) -> None:
    old_target = _target(
        session_scope_key="old-session",
        source_epoch="old-epoch",
    )
    new_target = _target(
        session_scope_key="new-session",
        source_epoch="new-epoch",
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "complete",
                "transaction_id": continuity_module._transaction_id(
                    external_projection_scope(old_target, evidence_count=1)
                ),
                "confirmed_login_scope": dict(old_target["evidence_scope"]),
            }
        ),
        encoding="utf-8",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=new_target,
    )

    status = owner.service()

    assert status.status == "no_source"
    assert status.phase == "archive_discovery"
    assert status.reason == "no exact-modem historical handoff is available"
    assert owner.generation() is None


def test_admitted_replacement_runs_after_superseded_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_target = _target(
        session_scope_key="old-session",
        source_epoch="old-epoch",
    )
    new_target = _target(
        session_scope_key="new-session",
        source_epoch="new-epoch",
    )
    old_transaction = continuity_module._transaction_id(
        external_projection_scope(old_target, evidence_count=1)
    )
    new_transaction = continuity_module._transaction_id(
        external_projection_scope(new_target, evidence_count=1)
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / continuity_module.STATUS_FILENAME).write_text(
        json.dumps(
            {
                "schema": continuity_module.SCHEMA,
                "owner": continuity_module.OWNER,
                "status": "superseded",
                "transaction_id": old_transaction,
                "confirmed_login_scope": dict(old_target["evidence_scope"]),
                "pending_transaction_id": new_transaction,
                "pending_preferred_sources": [str(tmp_path / "new-prior.zip")],
                "pending_force": False,
                "pending_max_age_days": 14,
                "pending_min_telemetry_rows": 100,
                "pending_confirmed_login_scope": dict(new_target["evidence_scope"]),
                "pending_confirmed_handoff_receipt": {},
            }
        ),
        encoding="utf-8",
    )
    owner = _service(
        stage_dir=tmp_path,
        memory_dir=memory,
        executor=_RejectingExecutor(),
        bulk_execution=_RejectingBulk(),
        target=new_target,
    )
    calls: list[HistoricalEvidenceImportRequest] = []

    def record_request(request):
        calls.append(request)
        return owner.status()

    monkeypatch.setattr(owner, "request", record_request)

    owner.service()
    assert calls[0].confirmed_login_scope.to_mapping() == new_target["evidence_scope"]


def test_chart_materialization_resolves_portable_stage_and_rotated_outputs(
    tmp_path: Path,
) -> None:
    now = time()
    target = _target(now=now)
    stage = tmp_path / "54GUC_Windows_portable_vB5_3"
    active = stage / "portable_runtime" / "guc_output"
    rotated = stage / "portable_runtime" / "guc_output_20260715_010203"
    active.mkdir(parents=True)
    rotated.mkdir(parents=True)
    for sequence, (root, timestamp) in enumerate(
        ((active, now - 60.0), (rotated, now - 3600.0)),
        1,
    ):
        raw = {
            "record_type": "tick",
            "ts": timestamp,
            "tick": sequence,
            "evidence_scope": b818_evidence_scope(
                formula_vector=telemetry_formula_vector(),
                provider_plmn="22005",
                session_scope_key=f"prior-session-{sequence}",
                source_epoch=f"prior-epoch-{sequence}",
                source_sequence=sequence,
                observed_at=timestamp,
            ),
        }
        (root / f"telemetry_production_2026071{sequence}.jsonl").write_text(
            json.dumps(build_production_telemetry_row(raw)) + "\n",
            encoding="utf-8",
        )
    target_scope = external_projection_scope(target, evidence_count=2).as_dict()
    pack = {
        "target_evidence_scope": target_scope,
        "facts": [
            {
                "promotion_status": "approved",
                "provenance": {"source_path": str(stage)},
            }
        ],
    }
    artifact_dir = tmp_path / "staging"
    receipt = materialize_historical_chart_artifact(
        artifact_dir,
        pack,
        max_age_days=14,
        transaction_id="continuity-stage-reader-proof",
    )

    assert receipt["admitted_rows"] == 2
    assert receipt["source_count"] == 1
    with gzip.open(
        artifact_dir / CHART_HISTORY_FILENAME,
        "rt",
        encoding="utf-8",
    ) as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert {row["tick"] for row in rows} == {1, 2}


def test_memory_only_handoff_publishes_honest_empty_chart_catalog(
    tmp_path: Path,
) -> None:
    target = _target()
    handoff = tmp_path / "next_start_handoff.zip"
    with zipfile.ZipFile(handoff, "w") as bundle:
        bundle.writestr(
            "portable_runtime/guc_memory/historical.json",
            json.dumps({"providers": {}}),
        )
    pack = {
        "target_evidence_scope": external_projection_scope(
            target,
            evidence_count=1,
        ).as_dict(),
        "facts": [
            {
                "promotion_status": "approved",
                "provenance": {"source_path": str(handoff)},
            }
        ],
    }
    receipt = materialize_historical_chart_artifact(
        tmp_path / "staging",
        pack,
        max_age_days=14,
        transaction_id="continuity-memory-only",
    )

    assert receipt["catalog_published"] is True
    assert receipt["admitted_rows"] == 0
    assert receipt["history_available"] is False
    assert receipt["reason"] == (
        "admitted sources contain no retained production telemetry"
    )


def test_manifest_cutoff_counts_only_rows_inside_14_day_window(tmp_path: Path) -> None:
    now = time()
    output = tmp_path / "portable_runtime" / "guc_output"
    output.mkdir(parents=True)
    target = _target(now=now)
    (output / "current_state.json").write_text(
        json.dumps({"runtime_identity": target}),
        encoding="utf-8",
    )
    rows = []
    for sequence, timestamp in enumerate(
        (now - 15 * 86400, now - 13 * 86400, now - 60),
        1,
    ):
        rows.append(
            {
                "record_type": "tick",
                "ts": timestamp,
                "tick": sequence,
                "evidence_scope": b818_evidence_scope(
                    formula_vector=telemetry_formula_vector(),
                    provider_plmn="22005",
                    source_sequence=sequence,
                    observed_at=timestamp,
                ),
            }
        )
    (output / "telemetry_production_20260716.jsonl").write_text(
        "".join(
            json.dumps(build_production_telemetry_row(row)) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    (output / "telemetry_history_20260716.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows * 10_000),
        encoding="utf-8",
    )

    manifest = build_external_memory_manifest(
        (output,),
        since_ts=now - 14 * 86400,
        telemetry_families=(FAMILY_PRODUCTION,),
    )

    assert manifest["summary"]["telemetry_rows"] == 2
    source = manifest["sources"][0]
    assert source["coverage"]["telemetry_rows"] == 2
    assert source["coverage"]["telemetry_file_names"] == [
        "telemetry_production_20260716.jsonl"
    ]


@pytest.mark.parametrize(
    "retired",
    (
        import_portable_state,
        import_next_start_handoff,
        auto_import_previous_portable_state,
    ),
)
def test_preidentity_state_copy_apis_fail_loudly(
    tmp_path: Path,
    retired,
) -> None:
    with pytest.raises(PortableStateConfirmedIdentityRequired):
        retired(tmp_path)


def test_static_audit_enforces_one_bulk_owner_and_no_startup_import() -> None:
    root = Path(__file__).resolve().parents[3]
    production_files = [
        path
        for path in (root / "arch4").rglob("*.py")
        if "tests" not in path.parts and "archive" not in path.parts
    ]
    importer_callers = []
    publisher_callers = []
    invalidation_callers = []
    retired_preidentity_callers = []
    handoff_receipt_validator_callers = []
    legacy_handoff_adapter_files = []
    commit_publishers = []
    for path in production_files:
        text = path.read_text(encoding="utf-8")
        if "automatic_external_memory_prior_import(" in text and path.name != "external_memory_auto_import.py":
            importer_callers.append(path.relative_to(root).as_posix())
        if "commit_staged_generation(" in text and path.name != "historical_continuity_store.py":
            commit_publishers.append(path.relative_to(root).as_posix())
        if "publish_external_memory_prior_register(" in text and path.name != "external_memory_runtime.py":
            publisher_callers.append(path.relative_to(root).as_posix())
        if "invalidate_external_memory_prior_register(" in text and path.name != "external_memory_runtime.py":
            invalidation_callers.append(path.relative_to(root).as_posix())
        if path.name != "portable_state_import.py" and any(
            token in text
            for token in (
                "import_portable_state(",
                "import_next_start_handoff(",
                "auto_import_previous_portable_state(",
            )
        ):
            retired_preidentity_callers.append(path.relative_to(root).as_posix())
        if (
            "validate_confirmed_handoff_continuity_receipt(" in text
            and path.name != "portable_state_import.py"
        ):
            handoff_receipt_validator_callers.append(
                path.relative_to(root).as_posix()
            )
        if "_confirmed_handoff_legacy_learning_prior(" in text:
            legacy_handoff_adapter_files.append(path.relative_to(root).as_posix())
    assert importer_callers == ["arch4/historical_continuity_process.py"]
    assert commit_publishers == ["arch4/intelligence/historical_evidence.py"]
    assert publisher_callers == []
    assert invalidation_callers == []
    assert retired_preidentity_callers == []
    assert handoff_receipt_validator_callers == [
        "arch4/external_memory_promotion.py"
    ]
    assert legacy_handoff_adapter_files == [
        "arch4/external_memory_learning_projection.py"
    ]
    orchestrator = (root / "arch4" / "ngui" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    product = (root / "arch4" / "product_supervisor.py").read_text(
        encoding="utf-8"
    )
    runtime_worker = (root / "arch4" / "ngui" / "runtime_worker.py").read_text(
        encoding="utf-8"
    )
    windows_launcher = (root / "arch4" / "windows_native_launcher.py").read_text(
        encoding="utf-8"
    )
    assert not (root / "arch4" / "trajectory_requests.py").exists()
    assert not (root / "arch4" / "engine_startup.py").exists()
    assert not (root / "arch4" / "engine_composition.py").exists()
    assert "automatic_external_memory_prior_import" not in orchestrator
    assert "submit_historical_evidence_import" not in orchestrator
    assert "_confirmed_handoff_path" not in orchestrator
    assert "confirmed_handoff_receipt" not in orchestrator
    assert "HistoricalEvidence" not in product
    assert "historical_continuity" not in product
    assert ").request(evidence_request)" not in runtime_worker
    assert "urgent_phase_completed" not in runtime_worker
    assert "handoff_path = str(request.payload" not in runtime_worker
    assert "--portable-import-state" not in windows_launcher
    owner_path = root / "arch4" / "intelligence" / "historical_evidence.py"
    owner_source = owner_path.read_text(encoding="utf-8")
    assert "urgent_phase_completed" not in owner_source
    assert "resolve_next_start_handoff_path" not in owner_source
    auto_import = (root / "arch4" / "external_memory_auto_import.py").read_text(
        encoding="utf-8"
    )
    assert "telemetry_families=(FAMILY_PRODUCTION,)" in auto_import
    assert "FAMILY_COMPARE" not in auto_import
    process_source = (
        root / "arch4" / "historical_continuity_process.py"
    ).read_text(encoding="utf-8")
    assert "arch4.engine" not in process_source
    assert "arch4.ngui" not in process_source
    assert "arch4.modem_live" not in process_source
    assert 'request.get("include_default_sources")' in process_source
    assert "include_default_sources=include_default_sources" in process_source
    assert "HistoricalContinuityChildFault(" in process_source
    assert 'fault_traceback = str(message.get("traceback") or "")' in process_source
    assert "MAX_CHILD_TRACEBACK_CHARS" in process_source
    assert "RAW_YIELD_EVERY_RECORDS = 200" in process_source
    assert '"child_traceback": exc.child_traceback' in owner_source
    assert '"bulk_execution": exc.error' in owner_source
    assert "include_default_sources=True" in (
        owner_source
    )
    for forbidden in (
        "arch4.engine",
        "arch4.brain",
        "arch4.smart_loop",
        "arch4.modem_live",
        "apply_external_memory_prior",
        "merge_external_knowledge_advisory",
    ):
        assert forbidden not in owner_source
    assert "include_default_sources=False" in (
        root / "arch4" / "tests" / "test_historical_continuity_store.py"
    ).read_text(encoding="utf-8")
    assert 'stage_text.startswith("/tmp/")' not in auto_import
    assert '"/pytest-" in stage_text' not in auto_import
    production_catches = []
    for path in production_files:
        text = path.read_text(encoding="utf-8")
        if "except HistoricalEvidenceRequestSuperseded" in text:
            production_catches.append(path.relative_to(root).as_posix())
    assert production_catches == []
