"""
arch4/modem/utils.py
═══════════════════════════════════════════════════════════════
Shared modem helper utilities.
"""

import os
import re
import subprocess
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional
import xml.etree.ElementTree as ET


HOST_PING_PROCESS_GRACE_S = 1.0


@dataclass(frozen=True, slots=True)
class RttProbeResult:
    """Transport result, before Modem Live assigns the existing probe sequence."""

    value_ms: float | None
    target: str
    elapsed_s: float = 0.0
    reason: str = ""
    exit_code: int | None = None
    detail: str = ""
    status: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.value_ms is not None and (
            type(self.value_ms) is not float
            or not math.isfinite(self.value_ms) or self.value_ms <= 0
        ):
            raise ValueError("rtt_probe_result_value_invalid")
        if not isinstance(self.target, str) or not self.target or len(self.target) > 255:
            raise ValueError("rtt_probe_result_target_invalid")
        if not math.isfinite(self.elapsed_s) or self.elapsed_s < 0:
            raise ValueError("rtt_probe_result_elapsed_invalid")
        if self.status not in {"ok", "failed", "unknown"}:
            raise ValueError("rtt_probe_result_status_invalid")
        if (
            (self.value_ms is None) != bool(self.reason)
            or (self.status == "ok") != (self.value_ms is not None)
        ):
            raise ValueError("rtt_probe_result_disposition_invalid")
        if len(self.detail) > 512:
            raise ValueError("rtt_probe_result_detail_unbounded")

    @property
    def failure_reason(self) -> str:
        if self.value_ms is not None:
            return ""
        return json.dumps({
            "reason": self.reason, "target": self.target, "status": self.status,
            "elapsed_s": round(self.elapsed_s, 3), "exit_code": self.exit_code,
            "detail": self.detail,
        }, sort_keys=True, separators=(",", ":"))


def xml_text(xml_str: str, tag: str, default: str = "") -> str:
    """Extract text content of a tag from XML string. Fast and safe."""
    try:
        root = ET.fromstring(xml_str)
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else default
    except ET.ParseError:
        return default


def xml_find(xml_str: str, tag: str) -> Optional[str]:
    """Find a tag value, return None if missing."""
    val = xml_text(xml_str, tag)
    return val if val else None


def build_host_ping_command(
    target: str,
    count: int = 3,
    ping_interface: Optional[str] = None,
    timeout_s: float = 2.0,
) -> list[str]:
    """Build an OS-appropriate host ping command."""
    samples = max(1, int(count))
    if os.name == "nt":
        timeout_ms = max(1000, int(float(timeout_s) * 1000))
        return ["ping", "-n", str(samples), "-w", str(timeout_ms), target]
    cmd = ["ping", "-c", str(samples), "-W", str(max(1, int(timeout_s)))]
    if ping_interface:
        cmd.extend(["-I", str(ping_interface)])
    cmd.append(target)
    return cmd


def host_ping_process_timeout_s(
    *,
    reply_timeout_s: float,
    count: int,
) -> float:
    """Return process lifetime without changing the per-reply deadline."""

    samples = max(1, int(count))
    reply_s = max(1.0, float(reply_timeout_s))
    return (samples * reply_s) + HOST_PING_PROCESS_GRACE_S


def ping_subprocess_kwargs(
    *,
    reply_timeout_s: float,
    count: int,
) -> dict[str, Any]:
    """Return bounded host-ping process settings without a Windows console."""
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": host_ping_process_timeout_s(
            reply_timeout_s=reply_timeout_s,
            count=count,
        ),
        "check": False,
    }
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startupinfo_factory and startf_use_showwindow:
            startupinfo = startupinfo_factory()
            startupinfo.dwFlags |= startf_use_showwindow
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
    return kwargs


def parse_ping_average_ms(output: str) -> Optional[float]:
    """Parse average RTT from common Linux/macOS/Windows ping outputs."""
    patterns = (
        r"rtt min/avg/max/mdev\s*=\s*[\d.]+/([\d.]+)/",
        r"round-trip min/avg/max(?:/stddev)?\s*=\s*[\d.]+/([\d.]+)/",
        r"Average\s*=\s*(\d+(?:\.\d+)?)\s*ms?",
    )
    for pattern in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def measure_host_ping_rtt(
    target: str = "8.8.8.8",
    count: int = 3,
    ping_interface: Optional[str] = None,
    timeout_s: float = 10.0,
) -> RttProbeResult:
    """Distinguish a completed network failure from an unmeasurable check."""
    cmd = build_host_ping_command(
        target=target,
        count=count,
        ping_interface=ping_interface,
        timeout_s=timeout_s,
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            **ping_subprocess_kwargs(
                reply_timeout_s=timeout_s,
                count=count,
            ),
        )
    except subprocess.TimeoutExpired as exc:
        return RttProbeResult(
            None, target, time.monotonic() - started, "process_timeout",
            detail=_ping_detail(exc.stdout, exc.stderr), status="unknown",
        )
    except OSError as exc:
        return RttProbeResult(None, target, time.monotonic() - started,
                              "host_os_error", detail=f"{type(exc).__name__}: {exc}"[:512],
                              status="unknown")
    elapsed = time.monotonic() - started
    output = _ping_detail(result.stdout, getattr(result, "stderr", ""), limit=None)
    detail = output[:512]
    value = parse_ping_average_ms(result.stdout or "")
    if result.returncode == 0 and value is not None and math.isfinite(value) and value > 0:
        return RttProbeResult(float(value), target, elapsed, exit_code=result.returncode,
                              status="ok")
    # A process exit alone says nothing about the Internet. Recognize only
    # explicit completed no-reply evidence; local or unfamiliar errors stay unknown.
    local_error = re.search(
        r"general failure|permission denied|operation not permitted|could not find host|"
        r"name or service not known|temporary failure in name resolution|"
        r"unknown host|invalid (?:argument|option)|usage:|connect: network is unreachable",
        output, re.IGNORECASE,
    )
    no_reply = re.search(
        r"request timed out|destination (?:host|net|network|port) unreachable|"
        r"100(?:\.0+)?% (?:packet )?loss",
        output, re.IGNORECASE,
    )
    positive_reply = value is not None and math.isfinite(value) and value > 0
    status = "failed" if no_reply and not local_error and not positive_reply else "unknown"
    reason = "ping_exit_failure" if result.returncode != 0 else "reply_unparseable"
    return RttProbeResult(None, target, elapsed, reason, result.returncode, detail,
                          status=status)


def _ping_detail(stdout: Any, stderr: Any, *, limit: int | None = 512) -> str:
    """TimeoutExpired can contain bytes even when the process uses text mode."""
    parts = [
        value.decode("utf-8", errors="replace") if isinstance(value, bytes)
        else str(value or "")
        for value in (stdout, stderr)
    ]
    return " ".join(" ".join(parts).split())[:limit]
