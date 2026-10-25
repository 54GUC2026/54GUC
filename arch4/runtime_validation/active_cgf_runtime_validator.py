"""Canonical active CGF runtime validation.

This module owns the product-level active runtime pass/fail decision.  It does
not replace the passive source-run observer or the Windows UI Automation probe;
those tools are explicit boundary adapters used by this validator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from arch4.atomic_io import atomic_write_json
from arch4.command_transactions import (
    COMMAND_AUTHORITY_STATE_FILENAME,
    CommandAuthorityStateRecord,
)
from arch4.foundation.live_artifact_reader import read_live_bytes


CGF_ACTIVE_RUNTIME_VALIDATION_SCHEMA = "CGFActiveRuntimeValidation.v1"
CGF_ACTIVE_RUNTIME_VALIDATION_OWNER = (
    "arch4.runtime_validation.active_cgf_runtime_validator"
)

PASSIVE_OBSERVER_ADAPTER = "scripts.cgf_windows_source_soak"
UIA_PROBE_ADAPTER = "scripts.ngui_live_ui_probe"
DEFAULT_WINDOWS_SOURCE_RUN_PYTHON = (
    r"D:\CODEX\54b\hp_source_run_venv\Scripts\python.exe"
)
VALIDATION_ENVIRONMENT_INTEROP_FAILURE = "validation_environment_interop"
VALIDATION_APPLICATION_UNAVAILABLE = "validation_application_unavailable"


@dataclass(frozen=True, slots=True)
class ValidationStepSpec:
    step_id: str
    owner: str
    adapter: str
    description: str
    argv: tuple[str, ...]
    mutates_modem: bool = False
    required: bool = True
    timeout_s: float | None = None
    depends_on: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["argv"] = list(self.argv)
        payload["depends_on"] = list(self.depends_on)
        return payload


CommandRunner = Callable[[Sequence[str], Path | None, float | None], subprocess.CompletedProcess[str]]


def command_authority_state_path(runtime_dir: str | Path) -> Path:
    """Return the sole bounded Command Authority current-state artifact."""

    return (
        Path(runtime_dir).resolve().parent
        / "guc_memory"
        / "cgf_state"
        / COMMAND_AUTHORITY_STATE_FILENAME
    )


def read_command_authority_state(
    runtime_dir: str | Path,
    *,
    required: bool = True,
) -> CommandAuthorityStateRecord | None:
    """Read and validate the one bounded Command Authority current state."""

    path = command_authority_state_path(runtime_dir)
    if not path.is_file():
        if required:
            raise RuntimeError(
                f"canonical Command Authority state is missing: {path}"
            )
        return None
    try:
        raw = json.loads(read_live_bytes(path).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"canonical Command Authority state has malformed JSON: {exc}"
        ) from exc
    if raw is None:
        raise RuntimeError(
            f"canonical Command Authority state disappeared while reading: {path}"
        )
    if not isinstance(raw, Mapping):
        raise RuntimeError(
            "canonical Command Authority state is not an object"
        )
    return CommandAuthorityStateRecord.from_mapping(raw)


def wait_command_authority_idle(
    runtime_dir: str | Path,
    *,
    reader: Callable[..., CommandAuthorityStateRecord | None] = read_command_authority_state,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> CommandAuthorityStateRecord:
    """Wait only for the exact active command owner's bounded terminal state."""

    state = reader(runtime_dir, required=True)
    if state is None:
        raise RuntimeError("canonical Command Authority state is unavailable")
    active = state.active_cellular
    if active is None:
        return state
    deadline = monotonic() + max(
        0.0,
        max(active.deadline_ts, active.lease.expires_ts) - wall_time(),
    ) + 10.0
    command_id = active.key.command_id
    while monotonic() < deadline:
        state = reader(runtime_dir, required=True)
        if state is None:
            raise RuntimeError("canonical Command Authority state disappeared")
        current = state.active_cellular
        if current is None:
            return state
        if current.key.command_id != command_id:
            raise RuntimeError(
                "Command Authority active command changed before terminal"
            )
        sleep(0.1)
    raise RuntimeError(
        f"Command Authority did not settle active command {command_id!r} "
        "before its owned deadline"
    )


def _default_command_runner(
    argv: Sequence[str],
    cwd: Path | None,
    timeout_s: float | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )


def _script(root: Path, name: str) -> str:
    return str((root / "scripts" / name).resolve())


def _windows_path(path: str | Path) -> str:
    """Return a Windows-readable path for WSL-hosted validation adapters."""

    if os.name == "nt":
        return str(path)
    raw = str(Path(path).resolve())
    if raw.startswith("/mnt/") and len(raw) > 6 and raw[6] == "/":
        drive = raw[5].upper()
        rest = raw[7:].replace("/", "\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu").strip() or "Ubuntu"
    return "\\\\wsl.localhost\\" + distro + raw.replace("/", "\\")


def _windows_python() -> str:
    if os.name == "nt":
        return _windows_python_contract_path()
    return (
        os.environ.get(
            "GUC_WINDOWS_UIA_PYTHON_LAUNCH",
            "",
        ).strip()
        or _wsl_launch_path(
            os.environ.get(
                "GUC_WINDOWS_UIA_PYTHON",
                DEFAULT_WINDOWS_SOURCE_RUN_PYTHON,
            ).strip()
            or DEFAULT_WINDOWS_SOURCE_RUN_PYTHON
        )
    )


def _wsl_launch_path(windows_executable: str) -> str:
    raw = str(windows_executable or "").strip()
    if len(raw) >= 3 and raw[1:3] == ":\\":
        drive = raw[0].lower()
        rest = raw[3:].replace("\\", "/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return raw


def _windows_python_contract_path() -> str:
    return os.environ.get(
        "GUC_WINDOWS_UIA_PYTHON",
        DEFAULT_WINDOWS_SOURCE_RUN_PYTHON,
    ).strip() or DEFAULT_WINDOWS_SOURCE_RUN_PYTHON


def _ui_probe_argv(
    *,
    root: Path,
    runtime_dir: Path,
    args: Iterable[str],
) -> tuple[str, ...]:
    converted_args: list[str] = []
    for arg in args:
        value = str(arg)
        if value.startswith("/"):
            converted_args.append(_windows_path(value))
        else:
            converted_args.append(value)
    return (
        _windows_python(),
        _windows_path(root / "scripts" / "ngui_live_ui_probe.py"),
        "--runtime-dir",
        _windows_path(runtime_dir),
        *tuple(converted_args),
    )


def _ui_probe_step(
    *,
    root: Path,
    step_id: str,
    description: str,
    runtime_dir: Path,
    args: Iterable[str],
    mutates_modem: bool = False,
    depends_on: tuple[str, ...] = (),
    timeout_s: float | None = None,
) -> ValidationStepSpec:
    return ValidationStepSpec(
        step_id=step_id,
        owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
        adapter=UIA_PROBE_ADAPTER,
        description=description,
        argv=_ui_probe_argv(root=root, runtime_dir=runtime_dir, args=args),
        mutates_modem=mutates_modem,
        timeout_s=(
            float(timeout_s)
            if timeout_s is not None
            else 300.0 if mutates_modem else 180.0
        ),
        depends_on=depends_on,
    )


def _validate_plan_dependencies(
    steps: Sequence[ValidationStepSpec],
) -> tuple[ValidationStepSpec, ...]:
    """Reject ambiguous ordering and mutation-readiness plans."""

    ordered = tuple(steps)
    by_id: dict[str, ValidationStepSpec] = {}
    ancestors: dict[str, set[str]] = {}
    previous_mutation = ""
    for step in ordered:
        if step.step_id in by_id:
            raise ValueError(f"duplicate_validation_step_id:{step.step_id}")
        if len(step.depends_on) != len(set(step.depends_on)):
            raise ValueError(
                f"duplicate_validation_step_dependency:{step.step_id}"
            )
        unknown = [dependency for dependency in step.depends_on if dependency not in by_id]
        if unknown:
            raise ValueError(
                f"validation_step_dependency_not_previous:{step.step_id}:"
                + ",".join(unknown)
            )
        inherited: set[str] = set(step.depends_on)
        for dependency in step.depends_on:
            inherited.update(ancestors[dependency])
        if (
            step.mutates_modem
            and previous_mutation
            and previous_mutation not in inherited
        ):
            raise ValueError(
                "mutating_validation_step_missing_prior_recovery_dependency:"
                f"{step.step_id}:{previous_mutation}"
            )
        by_id[step.step_id] = step
        ancestors[step.step_id] = inherited
        if step.mutates_modem:
            previous_mutation = step.step_id
    return ordered


def build_validation_plan(
    *,
    source_root: str | Path,
    runtime_dir: str | Path,
    output_dir: str | Path,
    platform: str,
    duration_s: float,
    interval_s: float = 5.0,
    include_sms: bool = False,
    require_mutating_radio: bool = False,
    graphs_ca_pool: str = "",
    developer_pband: str = "",
    developer_ca_pool: str = "",
) -> tuple[ValidationStepSpec, ...]:
    """Build the deterministic CGF active-runtime validation plan."""

    root = Path(source_root).resolve()
    runtime = Path(runtime_dir).resolve()
    output = Path(output_dir).resolve()
    platform_name = str(platform or "").strip().lower()
    if platform_name not in {"b818", "b535", "mikrotik", "generic"}:
        raise ValueError(f"unsupported_cgf_validation_platform:{platform!r}")
    if duration_s <= 0.0:
        raise ValueError("cgf_validation_duration_must_be_positive")
    if interval_s <= 0.0:
        raise ValueError("cgf_validation_interval_must_be_positive")
    if require_mutating_radio and not (graphs_ca_pool or developer_pband):
        raise ValueError("required_mutating_radio_arguments_missing")

    steps: list[ValidationStepSpec] = [
        ValidationStepSpec(
            step_id="top_level_window_audit",
            owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
            adapter=UIA_PROBE_ADAPTER,
            description="Audit that NGUI has no stray active top-level windows.",
            argv=_ui_probe_argv(
                root=root,
                runtime_dir=runtime,
                args=("--audit-top-level-windows",),
            ),
            timeout_s=60.0,
        ),
        ValidationStepSpec(
            step_id="visible_page_capture",
            owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
            adapter=UIA_PROBE_ADAPTER,
            description="Capture all visible NGUI pages and page-truth output.",
            argv=_ui_probe_argv(
                root=root,
                runtime_dir=runtime,
                args=(
                    "--capture-pages",
                    str(output / "visible_pages"),
                ),
            ),
            timeout_s=120.0,
        ),
        _ui_probe_step(
            root=root,
            step_id="graphs_safe_controls",
            description="Exercise Graphs page safe controls without modem mutation.",
            runtime_dir=runtime,
            args=("--exercise-graphs-safe-controls",),
        ),
        ValidationStepSpec(
            step_id="passive_runtime_observation",
            owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
            adapter=PASSIVE_OBSERVER_ADAPTER,
            description=(
                "Observe coherent current-state, runtime-health and page-truth "
                "artifacts after the visible Graphs surface is initialized."
            ),
            argv=(
                sys.executable,
                _script(root, "cgf_windows_source_soak.py"),
                str(runtime),
                "--duration-s",
                str(float(duration_s)),
                "--interval-s",
                str(float(interval_s)),
                "--progress-s",
                "30",
                "--output",
                str(output / "passive_runtime_observation.json"),
            ),
            timeout_s=float(duration_s) + 180.0,
        ),
        _ui_probe_step(
            root=root,
            step_id="site_report_current_run",
            description="Generate Foundation current-run Site Report.",
            runtime_dir=runtime,
            args=("--exercise-site-report",),
        ),
        _ui_probe_step(
            root=root,
            step_id="antenna_guide_non_actuating",
            description="Exercise Foundation non-actuating Antenna Guide controls.",
            runtime_dir=runtime,
            args=("--exercise-antenna-guide",),
            timeout_s=420.0,
        ),
        _ui_probe_step(
            root=root,
            step_id="traffic_plan_apply_restore",
            description="Apply and restore the Graphs traffic-plan settings.",
            runtime_dir=runtime,
            args=("--exercise-traffic-plan",),
        ),
        _ui_probe_step(
            root=root,
            step_id="band_sequence_plan_cancel",
            description="Plan and cancel a distant band sequence.",
            runtime_dir=runtime,
            args=("--exercise-band-sequence-plan-cancel",),
        ),
        _ui_probe_step(
            root=root,
            step_id="band_schedule_plan_cancel",
            description="Plan and cancel a distant band schedule.",
            runtime_dir=runtime,
            args=("--exercise-band-schedule-plan-cancel",),
        ),
        _ui_probe_step(
            root=root,
            step_id="ping_toggle_restore",
            description="Toggle downstream-health ping and restore its starting state.",
            runtime_dir=runtime,
            args=("--exercise-ping-toggle",),
        ),
    ]

    for confirmation_surface in (
        "graphs",
        "developer_control",
        "reboot",
        "shutdown",
        "band_auto",
        "band_lock",
        "cell_lock",
    ):
        steps.append(
            _ui_probe_step(
                root=root,
                step_id=f"confirm_transition_{confirmation_surface}",
                description=(
                    "Measure two-step confirmation arm/timeout without "
                    f"executing {confirmation_surface}."
                ),
                runtime_dir=runtime,
                args=("--measure-confirm-transition", confirmation_surface),
            )
        )

    for utility in (
        "version",
        "license",
        "buy_license",
        "updates",
        "scan_network",
        "support_bundle",
    ):
        steps.append(
            _ui_probe_step(
                root=root,
                step_id=f"device_utility_{utility}",
                description=f"Exercise Device utility {utility}.",
                runtime_dir=runtime,
                args=("--exercise-device-utility", utility),
            )
        )

    if include_sms:
        steps.extend(
            (
                _ui_probe_step(
                    root=root,
                    step_id="sms_toggle",
                    description="Exercise Foundation SMS ON/OFF control.",
                    runtime_dir=runtime,
                    args=("--exercise-sms-toggle",),
                ),
                _ui_probe_step(
                    root=root,
                    step_id="sms_read",
                    description="Exercise Foundation SMS receiver read surface.",
                    runtime_dir=runtime,
                    args=("--exercise-sms-read",),
                ),
            )
        )

    if graphs_ca_pool:
        steps.append(
            _ui_probe_step(
                root=root,
                step_id="manual_graphs_ca_pool",
                description="Exercise Graphs CA-pool manual radio action.",
                runtime_dir=runtime,
                args=("--exercise-graphs-ca-pool", str(graphs_ca_pool)),
                mutates_modem=True,
                depends_on=(
                    "passive_runtime_observation",
                    "confirm_transition_graphs",
                ),
            )
        )
        if developer_pband:
            steps.append(
                ValidationStepSpec(
                    step_id="manual_radio_user_cooldown_observation",
                    owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
                    adapter=PASSIVE_OBSERVER_ADAPTER,
                    description=(
                        "Observe live samples during the user manual-radio "
                        "cooldown before the next operator cellular action."
                    ),
                    argv=(
                        sys.executable,
                        _script(root, "cgf_windows_source_soak.py"),
                        str(runtime),
                        "--duration-s",
                        "35.0",
                        "--interval-s",
                        "5.0",
                        "--progress-s",
                        "35",
                        "--observation-contract",
                        "expected-radio-interruption",
                        "--output",
                        str(output / "manual_radio_user_cooldown_observation.json"),
                    ),
                    timeout_s=120.0,
                    depends_on=("manual_graphs_ca_pool",),
                )
            )
    if developer_pband:
        args = ["--exercise-developer-pband", str(developer_pband)]
        if developer_ca_pool:
            args.extend(("--developer-ca-pool", str(developer_ca_pool)))
        steps.append(
            _ui_probe_step(
                root=root,
                step_id="manual_developer_pband_ca_pool",
                description="Exercise Developer PBAND/CA-pool manual radio action.",
                runtime_dir=runtime,
                args=tuple(args),
                mutates_modem=True,
                depends_on=(
                    ("manual_radio_user_cooldown_observation",)
                    if graphs_ca_pool
                    else (
                        "passive_runtime_observation",
                        "confirm_transition_developer_control",
                    )
                ),
            )
        )
    final_mutation_step = (
        "manual_developer_pband_ca_pool"
        if developer_pband
        else ("manual_graphs_ca_pool" if graphs_ca_pool else "")
    )
    if final_mutation_step:
        steps.append(
            ValidationStepSpec(
                step_id="final_post_radio_observation",
                owner=CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
                adapter=PASSIVE_OBSERVER_ADAPTER,
                description=(
                    "Prove normal live delivery and New Horizon owner "
                    "coherence after the final operator radio action."
                ),
                argv=(
                    sys.executable,
                    _script(root, "cgf_windows_source_soak.py"),
                    str(runtime),
                    "--duration-s",
                    "35.0",
                    "--interval-s",
                    "5.0",
                    "--progress-s",
                    "35",
                    "--observation-contract",
                    "normal",
                    "--output",
                    str(output / "final_post_radio_observation.json"),
                ),
                timeout_s=120.0,
                depends_on=(final_mutation_step,),
            )
        )

    return _validate_plan_dependencies(steps)


def _parse_step_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[-2000:]


def _classify_step_failure(*, adapter: str, stderr: str, returncode: int | None) -> str:
    text = str(stderr or "")
    if (
        adapter == UIA_PROBE_ADAPTER
        and returncode not in (None, 0)
        and "pywinauto.timings.TimeoutError" in text
        and "window.wait" in text
    ):
        return VALIDATION_APPLICATION_UNAVAILABLE
    if (
        adapter == UIA_PROBE_ADAPTER
        and returncode not in (None, 0)
        and "UtilAcceptVsock" in text
        and "accept4 failed 110" in text
    ):
        return VALIDATION_ENVIRONMENT_INTEROP_FAILURE
    return "adapter_step_failed"


def _run_step_with_boundary_retry(
    *,
    runner: CommandRunner,
    step: ValidationStepSpec,
    root: Path,
    timeout: float | None,
    max_interop_retries: int,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    attempt_index = 0
    while True:
        started = time.time()
        completed = runner(step.argv, root, timeout)
        stderr = completed.stderr or ""
        returncode = int(completed.returncode)
        failure_class = (
            ""
            if returncode == 0
            else _classify_step_failure(
                adapter=step.adapter,
                stderr=stderr,
                returncode=returncode,
            )
        )
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "started_at": started,
                "completed_at": time.time(),
                "returncode": returncode,
                "failure_class": failure_class or None,
                "stderr_tail": stderr[-1000:],
            }
        )
        if (
            failure_class == VALIDATION_ENVIRONMENT_INTEROP_FAILURE
            and attempt_index < max(0, int(max_interop_retries))
        ):
            attempt_index += 1
            time.sleep(2.0)
            continue
        return completed, attempts


def run_active_runtime_validation(
    *,
    source_root: str | Path,
    runtime_dir: str | Path,
    output: str | Path,
    platform: str,
    duration_s: float,
    interval_s: float = 5.0,
    include_sms: bool = False,
    require_mutating_radio: bool = False,
    graphs_ca_pool: str = "",
    developer_pband: str = "",
    developer_ca_pool: str = "",
    runner: CommandRunner = _default_command_runner,
    minimum_step_timeout_s: float | None = None,
    collect_all_failures: bool = False,
    max_interop_retries: int = 1,
) -> dict[str, Any]:
    """Run the canonical active runtime validation and write one report."""

    if minimum_step_timeout_s is not None and minimum_step_timeout_s <= 0.0:
        raise ValueError("minimum_step_timeout_must_be_positive")

    started_at = time.time()
    output_path = Path(output).resolve()
    output_dir = output_path.parent
    plan = build_validation_plan(
        source_root=source_root,
        runtime_dir=runtime_dir,
        output_dir=output_dir,
        platform=platform,
        duration_s=duration_s,
        interval_s=interval_s,
        include_sms=include_sms,
        require_mutating_radio=require_mutating_radio,
        graphs_ca_pool=graphs_ca_pool,
        developer_pband=developer_pband,
        developer_ca_pool=developer_ca_pool,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []
    root = Path(source_root).resolve()
    results_by_step: dict[str, dict[str, Any]] = {}
    for step in plan:
        step_started = time.time()
        blocked_by = tuple(
            dependency
            for dependency in step.depends_on
            if not bool(results_by_step.get(dependency, {}).get("pass", False))
        )
        if blocked_by:
            result = {
                **step.to_payload(),
                "started_at": step_started,
                "completed_at": time.time(),
                "elapsed_s": 0.0,
                "effective_timeout_s": step.timeout_s,
                "returncode": None,
                "pass": False,
                "executed": False,
                "status": "blocked",
                "blocked_by": list(blocked_by),
                "failure_class": "prerequisite_failed",
                "attempts": [],
                "stdout": None,
                "stderr_tail": "",
                "error": (
                    f"step_prerequisite_failed:{step.step_id}:"
                    + ",".join(blocked_by)
                ),
            }
            results.append(result)
            results_by_step[step.step_id] = result
            step_failure = {
                "step_id": step.step_id,
                "adapter": step.adapter,
                "returncode": None,
                "failure_class": "prerequisite_failed",
                "error": result["error"],
            }
            failures.append(step_failure)
            if failure is None:
                failure = step_failure
            if not collect_all_failures:
                break
            continue
        try:
            timeout = step.timeout_s
            if minimum_step_timeout_s is not None:
                timeout = max(
                    float(timeout or 0.0),
                    float(minimum_step_timeout_s),
                )
            completed, attempts = _run_step_with_boundary_retry(
                runner=runner,
                step=step,
                root=root,
                timeout=timeout,
                max_interop_retries=max_interop_retries,
            )
            returncode = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            passed = returncode == 0
            failure_class = (
                None
                if passed
                else _classify_step_failure(
                    adapter=step.adapter,
                    stderr=stderr,
                    returncode=returncode,
                )
            )
            result = {
                **step.to_payload(),
                "started_at": step_started,
                "completed_at": time.time(),
                "elapsed_s": round(time.time() - step_started, 3),
                "effective_timeout_s": timeout,
                "returncode": returncode,
                "pass": passed,
                "executed": True,
                "status": "passed" if passed else "failed",
                "blocked_by": [],
                "failure_class": failure_class,
                "attempts": attempts,
                "stdout": _parse_step_stdout(stdout),
                "stderr_tail": stderr[-4000:],
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                **step.to_payload(),
                "started_at": step_started,
                "completed_at": time.time(),
                "elapsed_s": round(time.time() - step_started, 3),
                "effective_timeout_s": timeout,
                "returncode": None,
                "pass": False,
                "executed": True,
                "status": "failed",
                "blocked_by": [],
                "failure_class": "step_timeout",
                "attempts": [],
                "stdout": _parse_step_stdout(str(exc.stdout or "")),
                "stderr_tail": str(exc.stderr or "")[-4000:],
                "error": f"step_timeout:{step.step_id}",
            }
        results.append(result)
        results_by_step[step.step_id] = result
        if not result["pass"]:
            step_failure = {
                "step_id": step.step_id,
                "adapter": step.adapter,
                "returncode": result.get("returncode"),
                "failure_class": result.get("failure_class"),
                "error": result.get("error") or result.get("stderr_tail") or "step_failed",
            }
            failures.append(step_failure)
            if failure is None:
                failure = step_failure
            if (
                not collect_all_failures
                or result.get("failure_class")
                == VALIDATION_APPLICATION_UNAVAILABLE
            ):
                break

    report = {
        "schema": CGF_ACTIVE_RUNTIME_VALIDATION_SCHEMA,
        "owner": CGF_ACTIVE_RUNTIME_VALIDATION_OWNER,
        "status": "failed" if failure else "passed",
        "pass": failure is None,
        "platform": str(platform),
        "source_root": str(Path(source_root).resolve()),
        "runtime_dir": str(Path(runtime_dir).resolve()),
        "output": str(output_path),
        "started_at": started_at,
        "completed_at": time.time(),
        "elapsed_s": round(time.time() - started_at, 3),
        "step_count": len(plan),
        "result_count": len(results),
        "executed_step_count": sum(
            1 for result in results if result.get("executed") is True
        ),
        "blocked_step_count": sum(
            1 for result in results if result.get("status") == "blocked"
        ),
        "mutating_step_count": sum(1 for step in plan if step.mutates_modem),
        "adapters": sorted({step.adapter for step in plan}),
        "steps": [step.to_payload() for step in plan],
        "results": results,
        "failure": failure,
        "failures": failures,
        "collect_all_failures": bool(collect_all_failures),
    }
    atomic_write_json(output_path, report, sort_keys=True, trailing_newline=True)
    return report


def report_to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(dict(report), indent=2, sort_keys=True) + "\n"


def render_text_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"{report.get('schema')} · owner {report.get('owner')}",
        f"status: {report.get('status')}",
        f"platform: {report.get('platform')}",
        f"runtime: {report.get('runtime_dir')}",
        f"steps: {report.get('executed_step_count')}/{report.get('step_count')}",
    ]
    failure = report.get("failure")
    if isinstance(failure, Mapping):
        lines.append(
            "failure: "
            f"{failure.get('step_id')} via {failure.get('adapter')} "
            f"returncode={failure.get('returncode')}"
        )
    else:
        lines.append("failure: none")
    return "\n".join(lines) + "\n"


__all__ = (
    "CGF_ACTIVE_RUNTIME_VALIDATION_OWNER",
    "CGF_ACTIVE_RUNTIME_VALIDATION_SCHEMA",
    "PASSIVE_OBSERVER_ADAPTER",
    "UIA_PROBE_ADAPTER",
    "ValidationStepSpec",
    "build_validation_plan",
    "DEFAULT_WINDOWS_SOURCE_RUN_PYTHON",
    "VALIDATION_ENVIRONMENT_INTEROP_FAILURE",
    "VALIDATION_APPLICATION_UNAVAILABLE",
    "render_text_report",
    "report_to_json",
    "run_active_runtime_validation",
    "wait_command_authority_idle",
)
