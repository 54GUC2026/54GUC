"""Deterministic CGF architecture assessment.

The assessor is the single reporting surface over the active CGF architecture
gates.  It does not replace the test runner audits; it verifies that the required
audit population, active import graph and detached-customer boundaries are
still present and coherent.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


CGF_ARCHITECTURE_ASSESSMENT_SCHEMA = "CGFArchitectureAssessment.v1"
CGF_ARCHITECTURE_ASSESSMENT_OWNER = "arch4.architecture_assessor.cgf_assessor"

ACTIVE_ENTRY_MODULES: tuple[str, ...] = (
    "arch4.windows_native_launcher",
    "arch4.product_supervisor",
    "arch4.cgf_runner",
    "arch4.ngui.application",
    "arch4.new_horizon.runtime",
    "arch4.runner",
)

FORBIDDEN_ACTIVE_IMPORT_PREFIXES: tuple[str, ...] = (
    "arch4.engine",
    "arch4.controller_runtime",
    "arch4.controller_ports",
    "arch4.brain",
    "arch4.smart_loop",
    "arch4.intelligence",
    "arch4.telemetry",
    "arch4.reporting",
    "arch4.historical_",
    "arch4.external_memory",
    "arch4.portable_state_import",
)

DETACHED_PACKAGES: tuple[str, ...] = (
    "arch4.intelligence",
    "arch4.telemetry",
    "arch4.brain",
    "arch4.reporting",
    "arch4.smart_loop",
)

WINDOWS_PACKAGE_SPEC = "LUC-Windows-Native.spec"
REQUIRED_WINDOWS_PACKAGE_ROOTS: tuple[str, ...] = (
    "arch4.new_horizon",
)

REQUIRED_AUDIT_FILES: tuple[str, ...] = (
    "arch4/tests/cgf_verification_manifest.py",
    "arch4/tests/test_cgf_product_root_architecture.py",
    "arch4/tests/test_cgf_old_vocabulary_static_audit.py",
    "arch4/tests/test_cgf_evidence_writer_static_audit.py",
    "arch4/tests/test_cgf_deleted_owner_static_audit.py",
    "arch4/tests/test_cgf_large_code_review_manifest.py",
    "arch4/tests/test_cgf_runtime_register_writer_static_audit.py",
    "arch4/tests/test_cgf_100_percent_active_runtime_static_audit.py",
    "arch4/tests/test_cgf_worker_lifecycle_exclusivity.py",
    "arch4/tests/test_cgf_must_read_boundary.py",
    "arch4/tests/test_cgf_verification_manifest.py",
    "arch4/tests/test_cgf_active_runtime_validator.py",
)

REQUIRED_BEHAVIOR_PROOFS: tuple[str, ...] = (
    "product_without_intelligence",
    "every_core_register_has_one_exact_reachable_writer",
    "old_runtime_vocabulary_is_exactly_reviewed",
    "all_active_filesystem_writers_are_exact_bounded_and_reviewed",
    "modem_live_is_the_only_modem_io_owner",
    "all_active_page_buttons_have_typed_owner_routes",
    "foundation_report_support_and_update_utilities",
    "bounded_schema_owned_privacy_scrubbed_cgf_evidence",
    "central_cgf_architecture_assessment_is_available",
    "mandatory_active_cgf_runtime_validation_layer",
    "every_large_active_code_unit_is_explicitly_reviewed",
)

DELETED_PRODUCTION_PATHS: tuple[str, ...] = (
    "arch4/engine.py",
    "arch4/controller_runtime.py",
    "arch4/controller_ports.py",
    "arch4/reporting/site_report.py",
    "arch4/reporting/site_report_job.py",
)

ACTIVE_EVIDENCE_OWNER_MODULES: tuple[str, ...] = (
    "arch4.foundation.runtime_artifacts",
    "arch4.foundation.site_report",
    "arch4.foundation.site_report_job",
    "arch4.ngui.debug_surface",
    "arch4.ngui.fault_evidence",
    "arch4.core.configuration_journal",
    "arch4.core.correctness_journal",
    "arch4.memory_store.event_journal",
)

ACTIVE_MODULE_DISPOSITION_RULES: tuple[tuple[str, str], ...] = (
    ("arch4", "active package root"),
    ("arch4.accepted_", "accepted Modem Live generation contract/boundary"),
    ("arch4.action_", "Core action admission/shape/session contract"),
    ("arch4.command_", "Core command transaction contract"),
    ("arch4.control.", "Core manual-control support boundary"),
    ("arch4.customer_contracts.", "Foundation customer settings/handoff contract"),
    ("arch4.signal_processing.", "Core signal-processing authority"),
    ("arch4.ngui.", "active NGUI presentation/runtime boundary"),
    ("arch4.core.", "active Core current-state/action authority"),
    ("arch4.modem_live.", "active Modem Live matrix/translation authority"),
    ("arch4.new_horizon.", "required New Horizon Intelligence customer"),
    ("arch4.modem.", "Modem Live owned modem protocol adapter"),
    ("arch4.foundation.", "active Foundation current-run service/evidence owner"),
    ("arch4.runtime_validation.", "active CGF validation owner"),
    ("arch4.architecture_assessor.", "active CGF architecture assessment owner"),
    ("arch4.memory_store.", "bounded current-run storage adapter"),
    ("arch4.startup.", "startup readiness/check contract boundary"),
    ("arch4.architecture_contracts.", "cross-process lifecycle and generation contract"),
    ("arch4.core_contracts.", "Core public contract schema"),
    ("arch4.application_lease", "single-application lease authority"),
    ("arch4.app_info", "public app/version information boundary"),
    ("arch4.modem_live.connection_profiles", "Modem Live connection profile rows"),
    ("arch4.bouncer_receipt_contract", "Core bouncer receipt contract"),
    ("arch4.canonical_", "canonical display/JSON formatting utility"),
    ("arch4.coerce", "value coercion utility"),
    ("arch4.controller_frames", "current runtime frame schema boundary"),
    ("arch4.customer_presentation_extension", "Foundation customer presentation hook"),
    (
        "arch4.intelligence_presentation_contract",
        "optional Intelligence customer presentation contract",
    ),
    ("arch4.effective_policy_snapshot", "effective policy snapshot contract"),
    ("arch4.modem_live.profile_catalog", "Modem Live mutable profile boundary"),
    ("arch4.external_settings_decoder", "operator settings import decoder"),
    ("arch4.interfaces", "shared public interface contracts"),
    ("arch4.licensing", "license algorithm boundary"),
    ("arch4.live_seed_contract", "Modem Live seed contract"),
    ("arch4.log_processor", "memory-first runtime evidence batching owner"),
    ("arch4.lte_mcs", "LTE modulation coding lookup utility"),
    ("arch4.mutation_authority", "Core mutation authority contract"),
    ("arch4.open_source_manifest", "public open-source acknowledgement manifest"),
    ("arch4.operational_service_", "typed operational service status contract"),
    ("arch4.operator_profiles", "operator/provider profile seed data"),
    ("arch4.payload_budget", "bounded payload sizing utility"),
    ("arch4.protected_credentials", "Windows current-user DPAPI boundary; no modem truth ownership"),
    ("arch4.privacy_scrub", "privacy scrub boundary"),
    ("arch4.prepared_support", "shared stateless prepared-support privacy and receipt contract"),
    ("arch4.support_evidence", "shared read-only support projection and validation contract"),
    ("arch4.protected_operator_context", "protected operator context boundary"),
    ("arch4.radio_", "radio target/transition projection contract"),
    ("arch4.rtt_observation", "RTT observation contract"),
    ("arch4.signal_owner", "signal ownership contract"),
    ("arch4.signal_tuning_surface", "signal-tuning presentation contract"),
    ("arch4.trajectory_contract", "trajectory contract boundary"),
    ("arch4.updater", "explicit update-check boundary"),
    ("arch4.user_service_restoration", "manual-action user-service restoration contract"),
    ("arch4.versioning", "public version contract"),
    ("arch4.worker_health", "worker health state authority"),
    ("arch4.runtime_register", "runtime register contract/schema"),
    ("arch4.atomic_io", "sole durable filesystem adapter"),
    ("arch4.sqlite_recovery", "native SQLite corruption classification and preserving quarantine boundary"),
    ("arch4.event_journal", "controller event journal adapter"),
    ("arch4.fault_supervisor", "fault evidence classification owner"),
    ("arch4.identity", "identity decode/projection support"),
    ("arch4.license", "license credential/validation boundary"),
    ("arch4.profile", "static profile seed and provenance boundary"),
    ("arch4.runtime_", "runtime launch/path support boundary"),
    ("arch4.windows_", "Windows launcher/build boundary adapter"),
    ("arch4.portable_", "explicit portable diagnostic/package boundary"),
    ("arch4.preflight_", "explicit preflight diagnostic boundary"),
    ("arch4.support_bundle", "explicit support-bundle boundary"),
    ("arch4.cgf_runner", "active CGF product entrypoint"),
    ("arch4.runner", "active CLI/runtime entrypoint"),
    ("arch4.product_supervisor", "active product lifecycle supervisor"),
    (
        "arch4.product_operational_transaction",
        "Product operational idempotency and terminal receipt authority",
    ),
    (
        "arch4.product_intelligence",
        "single Product observation/outcome/proposal boundary for Intelligence",
    ),
    ("arch4.app_context", "active application context schema"),
    ("arch4.config", "active configuration schema"),
    ("arch4.safe_json", "JSON boundary adapter"),
    ("arch4.time", "time/clock utility boundary"),
    ("arch4.util", "shared utility boundary"),
    ("arch4.validation", "schema/value validation boundary"),
    ("arch4.wifi", "Modem Live owned Wi-Fi protocol support"),
    ("arch4.sms", "Foundation SMS boundary"),
)


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    code: str
    subject: str
    detail: str

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "subject": self.subject,
            "detail": self.detail,
        }


def _module_source(source_root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    module_path = source_root / relative.with_suffix(".py")
    if module_path.is_file():
        return module_path
    package_path = source_root / relative / "__init__.py"
    return package_path if package_path.is_file() else None


def _source_module(source_root: Path, path: Path) -> str:
    relative = path.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_arch4_imports(source_root: Path, path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current = _source_module(source_root, path)
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    result: set[str] = set()
    constants: dict[str, tuple[str, ...]] = {}
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for declaration in tree.body:
        if isinstance(declaration, ast.Assign):
            names = [target.id for target in declaration.targets if isinstance(target, ast.Name)]
            value = declaration.value
        elif isinstance(declaration, ast.AnnAssign) and isinstance(declaration.target, ast.Name):
            names, value = [declaration.target.id], declaration.value
        else:
            continue
        for name in names:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants[name] = (value.value,)

    def dynamic_values(argument: ast.AST, call: ast.Call) -> tuple[str, ...] | None:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return (argument.value,)
        if not isinstance(argument, ast.Name):
            return None
        # Resolve a preflight loop only inside that loop. An unrelated loop or
        # a local shadow must never supply a target for another import call.
        ancestor = parents.get(call)
        while ancestor is not None:
            if (isinstance(ancestor, ast.For) and isinstance(ancestor.target, ast.Name)
                    and ancestor.target.id == argument.id):
                if (isinstance(ancestor.iter, (ast.List, ast.Tuple))
                        and all(isinstance(item, ast.Constant) and isinstance(item.value, str)
                                for item in ancestor.iter.elts)
                        and not any(isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                                    and item.id == argument.id and item is not ancestor.target
                                    for item in ast.walk(ancestor))):
                    return tuple(item.value for item in ancestor.iter.elts)
                return None
            ancestor = parents.get(ancestor)
        writes = [item for item in ast.walk(tree)
                  if (isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                      and item.id == argument.id)
                  or (isinstance(item, ast.arg) and item.arg == argument.id)]
        return constants.get(argument.id) if len(writes) == 1 else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(
                alias.name for alias in node.names if alias.name.startswith("arch4")
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            keep = len(package_parts) - (node.level - 1)
            if keep < 0:
                continue
            prefix = ".".join(package_parts[:keep])
            imported = ".".join(
                part for part in (prefix, str(node.module or "")) if part
            )
        else:
            imported = str(node.module or "")
        if imported.startswith("arch4"):
            result.add(imported)
        for alias in node.names:
            candidate = ".".join(part for part in (imported, alias.name) if part)
            if candidate.startswith("arch4") and _module_source(source_root, candidate):
                result.add(candidate)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        target = ""
        if isinstance(node.func, ast.Name):
            target = node.func.id
        elif isinstance(node.func, ast.Attribute):
            target = node.func.attr
        if target not in {"import_module", "__import__"}:
            continue
        argument = node.args[0]
        values = dynamic_values(argument, node)
        if values is None:
            raise ValueError(f"architecture_dynamic_target_unresolved:{current}:{node.lineno}")
        for value in values:
            if value.startswith("arch4") and _module_source(source_root, value):
                result.add(value)
    return tuple(sorted(result))


def _reachable_modules(
    source_root: Path,
    entries: Iterable[str],
) -> dict[str, str | None]:
    parent: dict[str, str | None] = {entry: None for entry in entries}
    pending = list(entries)
    while pending:
        module = pending.pop(0)
        source = _module_source(source_root, module)
        if source is None:
            continue
        for imported in _resolved_arch4_imports(source_root, source):
            if imported in parent:
                continue
            parent[imported] = module
            pending.append(imported)
    return parent


def _import_chain(module: str, parent: Mapping[str, str | None]) -> str:
    chain: list[str] = []
    cursor: str | None = module
    while cursor is not None:
        chain.append(cursor)
        cursor = parent.get(cursor)
    return " <- ".join(chain)


def _is_forbidden_import(module: str) -> bool:
    return any(
        module.startswith(prefix)
        if prefix in {"arch4.engine", "arch4.historical_"}
        else module == prefix or module.startswith(prefix + ".")
        for prefix in FORBIDDEN_ACTIVE_IMPORT_PREFIXES
    )


def _load_behavior_manifest(source_root: Path) -> Mapping[str, tuple[str, ...]]:
    manifest_path = source_root / "arch4" / "tests" / "cgf_verification_manifest.py"
    if not manifest_path.is_file():
        return {}
    tree = ast.parse(
        manifest_path.read_text(encoding="utf-8"),
        filename=str(manifest_path),
    )
    for node in tree.body:
        target_names: tuple[str, ...] = ()
        if isinstance(node, ast.Assign):
            target_names = tuple(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = (node.target.id,)
            value = node.value
        else:
            continue
        if "CGF_BEHAVIOR_PROOF" not in target_names or value is None:
            continue
        loaded = ast.literal_eval(value)
        return {
            str(name): tuple(str(item) for item in files)
            for name, files in dict(loaded).items()
        }
    return {}


def _load_windows_package_roots(source_root: Path) -> tuple[str, ...]:
    spec_path = source_root / WINDOWS_PACKAGE_SPEC
    if not spec_path.is_file():
        return ()
    tree = ast.parse(
        spec_path.read_text(encoding="utf-8"),
        filename=str(spec_path),
    )
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name)
            and target.id == "CGF_HIDDEN_IMPORT_PACKAGES"
            for target in node.targets
        ):
            continue
        loaded = ast.literal_eval(node.value)
        if not isinstance(loaded, tuple) or any(
            type(item) is not str or not item.startswith("arch4.")
            for item in loaded
        ):
            raise ValueError("cgf_windows_package_roots_invalid")
        return tuple(loaded)
    return ()


def _module_disposition(module: str) -> str:
    for prefix, disposition in ACTIVE_MODULE_DISPOSITION_RULES:
        if prefix == "arch4":
            matches = module == prefix
        else:
            matches = module == prefix.rstrip(".") or module.startswith(prefix)
        if matches:
            return disposition
    return ""


def assess_cgf_architecture(source_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[2]
    root = root.resolve()
    violations: list[ArchitectureViolation] = []

    reachable = _reachable_modules(root, ACTIVE_ENTRY_MODULES)
    for entry in ACTIVE_ENTRY_MODULES:
        if _module_source(root, entry) is None:
            violations.append(
                ArchitectureViolation(
                    "active_entry_missing",
                    entry,
                    "active CGF entry module source is missing",
                )
            )

    forbidden_reachable = [
        module for module in sorted(reachable) if _is_forbidden_import(module)
    ]
    for module in forbidden_reachable:
        violations.append(
            ArchitectureViolation(
                "forbidden_active_import",
                module,
                _import_chain(module, reachable),
            )
        )

    audit_status: dict[str, dict[str, Any]] = {}
    for relative in REQUIRED_AUDIT_FILES:
        exists = (root / relative).is_file()
        audit_status[relative] = {"exists": exists}
        if not exists:
            violations.append(
                ArchitectureViolation(
                    "required_audit_file_missing",
                    relative,
                    "required CGF audit/test file is absent",
                )
            )

    deleted_status: dict[str, dict[str, Any]] = {}
    for relative in DELETED_PRODUCTION_PATHS:
        exists = (root / relative).exists()
        deleted_status[relative] = {"absent": not exists}
        if exists:
            violations.append(
                ArchitectureViolation(
                    "deleted_path_returned",
                    relative,
                    "retired production path exists",
                )
            )

    behavior_manifest = _load_behavior_manifest(root)
    behavior_status: dict[str, dict[str, Any]] = {}
    for behavior in REQUIRED_BEHAVIOR_PROOFS:
        files = tuple(behavior_manifest.get(behavior, ()))
        missing = tuple(
            file_name
            for file_name in files
            if not (root / "arch4" / "tests" / file_name).is_file()
        )
        behavior_status[behavior] = {
            "declared": bool(files),
            "files": list(files),
            "missing_files": list(missing),
        }
        if not files:
            violations.append(
                ArchitectureViolation(
                    "required_behavior_proof_missing",
                    behavior,
                    "required CGF behavior proof is not declared",
                )
            )
        for file_name in missing:
            violations.append(
                ArchitectureViolation(
                    "behavior_proof_file_missing",
                    behavior,
                    file_name,
                )
            )

    package_roots = _load_windows_package_roots(root)
    if not package_roots:
        violations.append(
            ArchitectureViolation(
                "windows_package_roots_missing",
                WINDOWS_PACKAGE_SPEC,
                "CGF_HIDDEN_IMPORT_PACKAGES is absent or empty",
            )
        )
    for package in REQUIRED_WINDOWS_PACKAGE_ROOTS:
        if package not in package_roots:
            violations.append(
                ArchitectureViolation(
                    "required_windows_package_root_missing",
                    package,
                    WINDOWS_PACKAGE_SPEC,
                )
            )

    detached_status: dict[str, dict[str, Any]] = {}
    for package in DETACHED_PACKAGES:
        present = (root / Path(*package.split("."))).is_dir()
        active = any(
            module == package or module.startswith(package + ".")
            for module in reachable
        )
        detached_status[package] = {
            "present": present,
            "active": active,
            "detached": present and not active,
            "packaged": package in package_roots,
        }
        if active:
            violations.append(
                ArchitectureViolation(
                    "detached_package_active",
                    package,
                    "detached package is reachable from active CGF entry graph",
                )
            )
        if package in package_roots:
            violations.append(
                ArchitectureViolation(
                    "detached_package_packaged",
                    package,
                    WINDOWS_PACKAGE_SPEC,
                )
            )

    evidence_status: dict[str, dict[str, Any]] = {}
    for module in ACTIVE_EVIDENCE_OWNER_MODULES:
        source = _module_source(root, module)
        reachable_from_active = module in reachable
        evidence_status[module] = {
            "source_exists": source is not None,
            "reachable_from_active": reachable_from_active,
        }
        if source is None:
            violations.append(
                ArchitectureViolation(
                    "active_evidence_owner_missing",
                    module,
                    "required evidence owner module is absent",
                )
            )

    module_dispositions: dict[str, dict[str, Any]] = {}
    for module in sorted(reachable):
        disposition = _module_disposition(module)
        module_dispositions[module] = {"disposition": disposition}
        if not disposition:
            violations.append(
                ArchitectureViolation(
                    "reachable_module_disposition_missing",
                    module,
                    "reachable active CGF module has no explicit disposition",
                )
            )

    payload = {
        "schema": CGF_ARCHITECTURE_ASSESSMENT_SCHEMA,
        "owner": CGF_ARCHITECTURE_ASSESSMENT_OWNER,
        "status": "failed" if violations else "passed",
        "source_root": str(root),
        "active_entry_modules": list(ACTIVE_ENTRY_MODULES),
        "reachable_module_count": len(reachable),
        "reachable_modules": sorted(reachable),
        "windows_package_roots": list(package_roots),
        "detached_packages": detached_status,
        "required_audit_files": audit_status,
        "required_behavior_proofs": behavior_status,
        "deleted_paths": deleted_status,
        "active_evidence_owners": evidence_status,
        "active_module_dispositions": module_dispositions,
        "violations": [violation.to_payload() for violation in violations],
    }
    return payload


def render_text_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"{report.get('schema')} · owner {report.get('owner')}",
        f"status: {report.get('status')}",
        f"source: {report.get('source_root')}",
        f"reachable modules: {report.get('reachable_module_count')}",
        "detached packages:",
    ]
    for name, status in sorted(dict(report.get("detached_packages") or {}).items()):
        state = "detached" if status.get("detached") else "VIOLATION"
        lines.append(f"  - {name}: {state}")
    violations = list(report.get("violations") or [])
    if violations:
        lines.append("violations:")
        for row in violations:
            lines.append(
                "  - "
                f"{row.get('code')}: {row.get('subject')} :: {row.get('detail')}"
            )
    else:
        lines.append("violations: none")
    return "\n".join(lines) + "\n"


def report_to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


__all__ = (
    "ACTIVE_ENTRY_MODULES",
    "ACTIVE_MODULE_DISPOSITION_RULES",
    "CGF_ARCHITECTURE_ASSESSMENT_OWNER",
    "CGF_ARCHITECTURE_ASSESSMENT_SCHEMA",
    "assess_cgf_architecture",
    "render_text_report",
    "report_to_json",
)
