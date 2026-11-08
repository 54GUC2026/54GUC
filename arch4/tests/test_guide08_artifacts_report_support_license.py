from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arch4.licensing import generate_license, validate_license
from arch4.open_source_manifest import load_open_source_manifest
from arch4.open_source_manifest import (
    OpenSourceManifestError,
    validate_open_source_manifest,
)
from arch4.privacy_scrub import SCRUB_MASK, scrub_record
from arch4.foundation.site_report_job import SiteReportContext, run_site_report_job
from arch4.core_contracts.site_report_history import SiteReportHistoryProjection
from arch4.support_bundle_context import SupportBundleContext
from arch4.support_bundle_job import run_support_bundle_job
from arch4.windows_native_launcher import create_support_bundle
from tools.generate_open_source_manifest import build_manifest


ROOT = Path(__file__).resolve().parents[2]


def _production_python() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "arch4").rglob("*.py")
        if "tests" not in path.relative_to(ROOT / "arch4").parts
    )


def _named_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_product_and_generator_use_only_the_vb53_hmac_contract() -> None:
    assert (
        validate_license(
            "MODEM-ONE",
            "A1B2C3D4E5F67890",
            "102D8E9F5A584707B178",
        )
        is True
    )
    nonce, key = generate_license("MODEM-ONE")
    assert len(nonce) == 16
    assert len(key) == 20
    assert key.isupper()
    assert validate_license("MODEM-ONE", nonce, key) is True

    product_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _production_python()
    )
    generator_text = (ROOT / "tools/guc_license_gen.py").read_text(encoding="utf-8")
    packaging_text = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "LUC-Windows-Native.spec",
            "tools/guc_license_gen.spec",
            "GUC_License_Gen.spec",
        )
    )
    current_contract_text = "\n".join((product_text, generator_text, packaging_text))
    for banned in (
        "ED1.",
        "Ed25519",
        "ed25519",
        "private_license_signing",
        "_PRIVATE_SIGNING_KEY_HEX",
        "LICENSE_PUBLIC_KEY_HEX",
        "decode_license_signature",
        "encode_license_signature",
    ):
        assert banned not in current_contract_text

    assert "generate_license as _arch4_generate_license" in generator_text
    assert "_derive_key as _arch4_derive_key" in generator_text
    assert "hmac.new" not in generator_text
    product_spec = (ROOT / "LUC-Windows-Native.spec").read_text(encoding="utf-8")
    assert "private_license_signing" not in product_spec

    validate_callers = []
    credential_writers = []
    for path in _production_python():
        relative = path.relative_to(ROOT).as_posix()
        if relative != "arch4/licensing.py" and "validate_license(" in path.read_text(
            encoding="utf-8"
        ):
            validate_callers.append(relative)
        if (
            relative != "arch4/license_credential_store.py"
            and "save_license_credentials(" in path.read_text(encoding="utf-8")
        ):
            credential_writers.append(relative)
    assert validate_callers == ["arch4/foundation/operational_services.py"]
    assert credential_writers == ["arch4/foundation/operational_services.py"]

    parse_callers = []
    for path in _production_python():
        relative = path.relative_to(ROOT).as_posix()
        if relative != "arch4/licensing.py" and "parse_license_entry(" in path.read_text(
            encoding="utf-8"
        ):
            parse_callers.append(relative)
    assert parse_callers == ["arch4/foundation/operational_services.py"]


def test_generated_dependency_manifest_exactly_matches_checked_runtime_truth() -> None:
    checked = json.loads(
        (ROOT / "open_source_manifest.json").read_text(encoding="utf-8")
    )
    assert checked == build_manifest(ROOT)
    components = load_open_source_manifest(ROOT)
    assert {item.name for item in components} >= {
        "Python",
        "PySide6",
        "pyqtgraph",
        "requests",
        "RouterOS-api",
        "PyInstaller",
        "pytest",
    }


def test_dependency_manifest_fingerprint_blocks_hand_edits() -> None:
    payload = build_manifest(ROOT)
    payload["components"][0]["license"] = "invented"
    with pytest.raises(
        OpenSourceManifestError,
        match="open_source_manifest_fingerprint_mismatch",
    ):
        validate_open_source_manifest(payload)


def test_support_job_succeeds_from_typed_source_context(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    memory = tmp_path / "memory"
    runtime.mkdir()
    memory.mkdir()
    (runtime / "current_state.json").write_text("{}", encoding="utf-8")
    context = SupportBundleContext.from_runtime_paths(
        app_root=tmp_path,
        runtime_dir=runtime,
        memory_dir=memory,
        source="test_source_run",
    )

    artifact = run_support_bundle_job(context, exporter=create_support_bundle)

    assert artifact.is_file()
    assert not (runtime / "support_bundle_failure_receipt.json").exists()


def test_privacy_scrubber_removes_last_sms_text_sender_and_approved_numbers() -> None:
    clean = scrub_record(
        {
            "inbox": {
                "latest_message": {
                    "message_id": "7",
                    "sender": "+381641234567",
                    "text": "private customer message",
                    "timestamp": "2026-07-17 12:00:00",
                    "protocol": "huawei_hilink_xml",
                }
            },
            "sms_command_approved_senders": ["+381641234567"],
        }
    )
    latest = clean["inbox"]["latest_message"]
    assert latest["sender"] == SCRUB_MASK
    assert latest["text"] == SCRUB_MASK
    assert clean["sms_command_approved_senders"] == SCRUB_MASK


def test_support_job_reports_scrubbed_failure_without_alternate_exporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    memory = tmp_path / "memory"
    runtime.mkdir()
    memory.mkdir()
    context = SupportBundleContext.from_runtime_paths(
        app_root=tmp_path,
        runtime_dir=runtime,
        memory_dir=memory,
        source="test_source_run",
    )

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("password=Secret123 phone=+381641234567")

    with pytest.raises(RuntimeError, match="Secret123"):
        run_support_bundle_job(context, exporter=fail_export)

    receipt = (runtime / "support_bundle_failure_receipt.json").read_text(
        encoding="utf-8"
    )
    assert "Secret123" not in receipt
    assert "+381641234567" not in receipt
    assert "SupportBundleFailureReceipt.v1" in receipt


class _ResultQueue:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    def get(self, *, timeout: float) -> dict[str, object]:
        assert timeout == 1.0
        return self._result


class _CompletedProcess:
    exitcode = 1

    def start(self) -> None:
        return None

    def join(self, _timeout: float) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class _FailedProcessContext:
    def Queue(self, *, maxsize: int):  # noqa: N802 - multiprocessing API
        del maxsize
        return _ResultQueue(
            {"ok": False, "error": "password=Secret123 phone=+381641234567"}
        )

    def Process(self, **_kwargs):  # noqa: N802 - multiprocessing API
        return _CompletedProcess()


def test_site_report_job_requires_typed_context_and_writes_scrubbed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    memory = tmp_path / "memory"
    runtime.mkdir()
    memory.mkdir()
    support = SupportBundleContext.from_runtime_paths(
        app_root=tmp_path,
        runtime_dir=runtime,
        memory_dir=memory,
    )
    context = SiteReportContext.from_support_context(support)
    monkeypatch.setattr(
        "arch4.foundation.site_report_job.multiprocessing.get_context",
        lambda _method: _FailedProcessContext(),
    )

    with pytest.raises(TypeError, match="site_report_context_required"):
        run_site_report_job(
            context=SimpleNamespace(),  # type: ignore[arg-type]
            hours=1.0,
            history=SiteReportHistoryProjection.unavailable(
                requested_hours=1.0,
                generated_at=1.0,
                reason="test",
            ),
            open_browser=False,
        )
    with pytest.raises(RuntimeError, match="Secret123"):
        run_site_report_job(
            context=context,
            hours=1.0,
            history=SiteReportHistoryProjection.unavailable(
                requested_hours=1.0,
                generated_at=1.0,
                reason="test",
            ),
            open_browser=False,
        )

    receipt = (runtime / "site_report_failure_receipt.json").read_text(encoding="utf-8")
    assert "Secret123" not in receipt
    assert "+381641234567" not in receipt
    assert "SiteReportFailureReceipt.v1" in receipt


def test_static_utility_callers_use_only_typed_jobs() -> None:
    support_callers: list[str] = []
    report_callers: list[str] = []
    for path in _production_python():
        calls = _named_calls(path)
        relative = path.relative_to(ROOT).as_posix()
        if "create_support_bundle" in calls:
            support_callers.append(relative)
        if "generate_site_report" in calls:
            report_callers.append(relative)
    assert support_callers == []
    support_job_callers = []
    for path in _production_python():
        if "run_support_bundle_job" in _named_calls(path):
            support_job_callers.append(path.relative_to(ROOT).as_posix())
    assert support_job_callers == [
        "arch4/foundation/operational_services.py",
    ]
    assert report_callers == []
    foundation_report_callers = []
    for path in _production_python():
        if "generate_foundation_site_report" in _named_calls(path):
            foundation_report_callers.append(path.relative_to(ROOT).as_posix())
    assert foundation_report_callers == ["arch4/foundation/site_report_job.py"]


def test_static_old_credential_reads_fail_and_cannot_compete_with_store() -> None:
    user_config = (ROOT / "arch4/user_config.py").read_text(encoding="utf-8")
    tree = ast.parse(user_config)
    function_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for function_name in ("load_license_dat", "load_license_tier_dat"):
        assert function_name not in function_names
    for path in _production_python():
        assert "load_license_dat(" not in path.read_text(encoding="utf-8")
