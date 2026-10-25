"""One profile-owned admission point for every physical modem request.

Vendor backends still encode the wire request, but they must enter this
authority immediately before touching the wire.  The resolved Modem Live
access profile is the only source of the minimum request gap.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import threading
import time
from typing import Any, Callable, Iterator

from arch4.modem.live_lane_contract import ModemLiveAccessProfile
from arch4.modem_live.access_scheduler import (
    MODEM_LIVE_ACCESS_LANES,
    lane_priority,
)


PHYSICAL_REQUEST_LANES = MODEM_LIVE_ACCESS_LANES.difference({"export"})

# Export is a Modem Live call context but never a legal physical request. It is
# admitted to lane_scope so a snapshot can run, then rejected by request() if a
# backend accidentally reaches the wire while exporting cached truth.
MODEM_LIVE_REQUEST_CONTEXT_LANES = MODEM_LIVE_ACCESS_LANES

# Read-only projection of the one scheduler-owned priority table.
# It is exported for diagnostics/tests, but contains no independent values.
PHYSICAL_REQUEST_LANE_PRIORITY = {
    lane: lane_priority(lane) for lane in sorted(PHYSICAL_REQUEST_LANES)
}


class ModemLivePhysicalRequestAuthority:
    """Serialize and pace one modem session at the physical wire boundary."""

    def __init__(
        self,
        profile: ModemLiveAccessProfile,
        *,
        journal_limit: int = 512,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(profile, ModemLiveAccessProfile):
            raise TypeError("modem_live_access_profile_required")
        gap_s = float(profile.min_inter_request_gap_s)
        if gap_s < 0.0:
            raise ValueError("modem_live_physical_request_gap_negative")
        limit = int(journal_limit)
        if limit < 8:
            raise ValueError("modem_live_physical_request_journal_too_small")
        self._profile = profile
        self._gap_s = gap_s
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._condition = threading.Condition(threading.RLock())
        self._active = False
        self._last_finished_mono: float | None = None
        self._last_request_gap_s = gap_s
        self._sequence = 0
        self._waiter_sequence = 0
        self._waiters: dict[int, tuple[int, str]] = {}
        self._lane_local = threading.local()
        self._journal: deque[dict[str, Any]] = deque(maxlen=limit)
        self._gap_violation_count = 0

    @property
    def profile_id(self) -> str:
        return str(self._profile.profile_id)

    @property
    def minimum_gap_s(self) -> float:
        return float(self._gap_s)

    @staticmethod
    def _require_label(value: Any, *, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"modem_live_physical_request_{field}_required")
        return text

    @contextmanager
    def lane_scope(self, lane: str) -> Iterator[None]:
        lane_name = self._require_label(lane, field="lane")
        if lane_name not in MODEM_LIVE_REQUEST_CONTEXT_LANES:
            raise ValueError(
                f"modem_live_physical_request_lane_invalid:{lane_name}"
            )
        previous = getattr(self._lane_local, "lane", "")
        previous_request = getattr(self._lane_local, "last_request_sequence", 0)
        self._lane_local.lane = lane_name
        try:
            yield
        finally:
            if (lane_name == "fast_live" and previous != lane_name
                    and self._profile.dev2_state_read_gap_s == 0.0
                    and getattr(self._lane_local, "last_request_sequence", 0) != previous_request):
                # Ordinary DEV2 calls stay consecutive within this block.
                # Its next wire caller must respect the existing profile gap
                # from the last physical completion, including failed reads.
                with self._condition:
                    self._last_request_gap_s = max(self._last_request_gap_s, self._gap_s)
            self._lane_local.lane = previous

    def current_lane(self, *, default: str) -> str:
        return str(getattr(self._lane_local, "lane", "") or default)

    @contextmanager
    def request(
        self, *, lane: str, operation: str, request_kind: str = "protected"
    ) -> Iterator[dict[str, Any]]:
        lane_name = self._require_label(lane, field="lane")
        operation_name = self._require_label(operation, field="operation")
        if request_kind == "routeros_state_read":
            configured_gap = self._profile.routeros_state_read_gap_s
            if configured_gap is None:
                raise ValueError("modem_live_routeros_state_read_profile_required")
            request_gap_s = float(configured_gap)
        elif request_kind == "dev2_state_read":
            configured_gap = self._profile.dev2_state_read_gap_s
            if configured_gap is None:
                raise ValueError("modem_live_dev2_state_read_profile_required")
            request_gap_s = float(configured_gap)
        elif request_kind == "protected":
            request_gap_s = self._gap_s
        else:
            raise ValueError("modem_live_physical_request_kind_invalid")
        if request_gap_s < 0.0:
            raise ValueError("modem_live_physical_request_gap_negative")
        if lane_name not in PHYSICAL_REQUEST_LANES:
            raise ValueError(
                f"modem_live_physical_request_lane_invalid:{lane_name}"
            )

        priority = int(PHYSICAL_REQUEST_LANE_PRIORITY[lane_name])
        wait_started_mono = self._monotonic()
        with self._condition:
            self._waiter_sequence += 1
            waiter_id = int(self._waiter_sequence)
            self._waiters[waiter_id] = (priority, lane_name)
            self._condition.notify_all()

        try:
            while True:
                gap_wait_s = 0.0
                with self._condition:
                    next_waiter = min(
                        self._waiters,
                        key=lambda current: (
                            self._waiters[current][0],
                            current,
                        ),
                    )
                    if self._active or next_waiter != waiter_id:
                        self._condition.wait()
                        continue
                    if self._last_finished_mono is not None:
                        # A cheap read cannot shorten the quiet interval after
                        # a monitor, AT command or write.
                        required_gap_s = max(request_gap_s, self._last_request_gap_s)
                        elapsed_s = max(
                            0.0,
                            self._monotonic() - self._last_finished_mono,
                        )
                        gap_wait_s = max(0.0, required_gap_s - elapsed_s)
                    if gap_wait_s <= 0.0:
                        self._waiters.pop(waiter_id, None)
                        self._active = True
                        break

                # Do not reserve the wire while waiting for the profile gap.
                # Short slices let a newly queued action/recovery request take
                # priority before the next physical request starts.
                self._sleeper(min(gap_wait_s, 0.01))
        except BaseException:
            with self._condition:
                self._waiters.pop(waiter_id, None)
                self._condition.notify_all()
            raise

        started_mono = self._monotonic()
        required_wait_s = max(0.0, started_mono - wait_started_mono)
        previous_finished = self._last_finished_mono
        required_gap_s = max(request_gap_s, self._last_request_gap_s)
        observed_gap_s = (
            None
            if previous_finished is None
            else max(0.0, started_mono - previous_finished)
        )
        # A real sleeper may return slightly early. Finish the remainder once;
        # never send a request before the profile boundary.
        if observed_gap_s is not None and observed_gap_s + 0.0005 < required_gap_s:
            remainder_s = required_gap_s - observed_gap_s
            self._sleeper(remainder_s)
            started_mono = self._monotonic()
            observed_gap_s = max(0.0, started_mono - previous_finished)
        if observed_gap_s is not None and observed_gap_s + 0.0005 < required_gap_s:
            with self._condition:
                self._active = False
                self._gap_violation_count += 1
                self._condition.notify_all()
            raise RuntimeError("modem_live_physical_request_gap_not_enforced")

        with self._condition:
            self._sequence += 1
            sequence = self._sequence
        entry = {
            "sequence": sequence,
            "profile_id": self.profile_id,
            "lane": lane_name,
            "operation": operation_name,
            "started_at": float(self._wall_clock()),
            "minimum_gap_s": round(required_gap_s, 3),
            "request_kind": request_kind,
            "observed_gap_s": (
                None if observed_gap_s is None else round(observed_gap_s, 6)
            ),
            "waited_s": round(required_wait_s, 6),
            "ok": False,
            "failure": "",
        }
        try:
            yield entry
            entry["ok"] = True
        except BaseException as exc:
            entry["failure"] = type(exc).__name__
            raise
        finally:
            finished_mono = self._monotonic()
            entry["finished_at"] = float(self._wall_clock())
            entry["elapsed_s"] = round(max(0.0, finished_mono - started_mono), 6)
            with self._condition:
                self._last_finished_mono = finished_mono
                self._last_request_gap_s = request_gap_s
                self._lane_local.last_request_sequence = sequence
                self._journal.append(dict(entry))
                self._active = False
                self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "owner": "modem_live_physical_request_authority",
                "profile_id": self.profile_id,
                "minimum_gap_s": round(self._gap_s, 3),
                "active": bool(self._active),
                "waiting_count": len(self._waiters),
                "waiting_by_lane": {
                    lane: sum(
                        1
                        for _priority, waiting_lane in self._waiters.values()
                        if waiting_lane == lane
                    )
                    for lane in sorted(
                        {waiting_lane for _priority, waiting_lane in self._waiters.values()}
                    )
                },
                "request_count": int(self._sequence),
                "gap_violation_count": int(self._gap_violation_count),
                "journal": [dict(item) for item in self._journal],
            }


__all__ = [
    "ModemLivePhysicalRequestAuthority",
    "PHYSICAL_REQUEST_LANES",
    "MODEM_LIVE_REQUEST_CONTEXT_LANES",
    "PHYSICAL_REQUEST_LANE_PRIORITY",
]
