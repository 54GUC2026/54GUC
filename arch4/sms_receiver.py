"""Canonical SMS inbox and command-policy authorities.

Modem families translate native protocol into :class:`SmsMessage` records.
The inbox authority owns acquisition and latest-message truth.  The command
policy authority independently owns authorization and command interpretation.
Neither authority executes modem actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
import hashlib
import json
import logging
import math
from pathlib import Path
from queue import Empty, SimpleQueue
import re
from threading import RLock
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from arch4.coerce import safe_bool
from arch4.atomic_io import atomic_write_json

log = logging.getLogger("arch4.sms")


def _drain_sms_events(events: SimpleQueue) -> list[dict[str, Any]]:
    result = []
    while True:
        try:
            result.append(events.get_nowait())
        except Empty:
            return result


@dataclass(frozen=True)
class SmsMessage:
    message_id: str
    sender: str = ""
    text: str = ""
    timestamp: str = ""
    read: bool = False
    protocol: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SmsCommand:
    kind: str
    _params_json: str = "{}"
    raw: str = ""
    command_id: str = ""
    message_id: str = ""
    sender: str = ""
    timestamp: str = ""
    protocol: str = ""
    received_ts: float = 0.0

    @property
    def params(self) -> dict[str, Any]:
        return json.loads(self._params_json)

    @classmethod
    def build(
        cls,
        kind: str,
        *,
        params: dict[str, Any] | None = None,
        raw: str = "",
        command_id: str = "",
        message_id: str = "",
        sender: str = "",
        timestamp: str = "",
        protocol: str = "",
        received_ts: float = 0.0,
    ) -> "SmsCommand":
        return cls(
            kind=str(kind or "").strip().upper(),
            _params_json=json.dumps(
                dict(params or {}),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            raw=str(raw or ""),
            command_id=str(command_id or ""),
            message_id=str(message_id or ""),
            sender=str(sender or ""),
            timestamp=str(timestamp or ""),
            protocol=str(protocol or ""),
            received_ts=float(received_ts or 0.0),
        )

    def with_message(
        self,
        message: SmsMessage,
        *,
        normalized_sender: str,
        received_ts: float,
    ) -> "SmsCommand":
        # Inbox slots can be renumbered after a modem restart. They are delete
        # addresses, not message identities. The received instant also survives
        # equivalent timestamp formatting and changes to command configuration.
        if not math.isfinite(received_ts) or received_ts <= 0:
            raise ValueError("sms_received_timestamp_invalid")
        material = json.dumps(
            [str(normalized_sender or ""), round(received_ts, 3),
             str(message.text or "").strip()],
            ensure_ascii=True, separators=(",", ":"),
        )
        command_id = "sms:" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()
        return SmsCommand.build(
            self.kind,
            params=self.params,
            raw=self.raw,
            command_id=command_id,
            message_id=str(message.message_id or ""),
            sender=normalized_sender,
            timestamp=str(message.timestamp or ""),
            protocol=str(message.protocol or ""),
            received_ts=received_ts,
        )


@dataclass(frozen=True)
class SmsInboxPoll:
    polled: bool
    messages: tuple[SmsMessage, ...] = ()


@runtime_checkable
class SmsTransport(Protocol):
    protocol: str

    def read_messages(self, *, limit: int = 20, unread_only: bool = False) -> list[SmsMessage]:
        ...

    def send_message(self, number: str, text: str) -> bool:
        ...

    def delete_message(self, message_id: str) -> bool:
        ...


class NullSmsTransport:
    protocol = "unsupported"

    def __init__(self, reason: str = "sms_not_supported") -> None:
        self.reason = str(reason or "sms_not_supported")

    def read_messages(self, *, limit: int = 20, unread_only: bool = False) -> list[SmsMessage]:
        del limit, unread_only
        return []

    def send_message(self, number: str, text: str) -> bool:
        del number, text
        return False

    def delete_message(self, message_id: str) -> bool:
        del message_id
        return False


class LiveModuleSmsTransport:
    """Transport that asks Modem Live for canonical SMS messages first."""

    protocol = "modem_live_canonical"

    def __init__(self, live_module: Any):
        self._live_module = live_module

    def read_messages(self, *, limit: int = 20, unread_only: bool = False) -> list[SmsMessage]:
        reader = getattr(self._live_module, "read_sms_messages", None)
        if not callable(reader):
            return []
        raw_messages = reader(limit=limit, unread_only=unread_only) or []
        return [_coerce_message(item, protocol=self.protocol) for item in list(raw_messages or [])]

    def send_message(self, number: str, text: str) -> bool:
        sender = getattr(self._live_module, "send_sms_message", None)
        return bool(callable(sender) and sender(str(number or ""), str(text or "")))

    def delete_message(self, message_id: str) -> bool:
        deleter = getattr(self._live_module, "delete_sms_message", None)
        return bool(callable(deleter) and deleter(str(message_id or "")))


class ReceiveSmsTransport(LiveModuleSmsTransport):
    """Reuse canonical inbox/command policy, with no outbound SMS operations."""

    def send_message(self, number: str, text: str) -> bool:
        raise RuntimeError('sms_receive_only_send_not_implemented')

    def delete_message(self, message_id: str) -> bool:
        raise RuntimeError('sms_receive_only_delete_not_implemented')


class HilinkSmsTransport:
    """Huawei HiLink XML adapter producing canonical SmsMessage records."""

    protocol = "huawei_hilink_xml"

    def __init__(self, live_module: Any):
        self._live_module = live_module

    def read_messages(self, *, limit: int = 20, unread_only: bool = False) -> list[SmsMessage]:
        read_count = max(1, min(50, int(limit or 20)))
        unread_pref = "1" if unread_only else "0"
        xml_body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<request>"
            "<PageIndex>1</PageIndex>"
            f"<ReadCount>{read_count}</ReadCount>"
            "<BoxType>1</BoxType>"
            "<SortType>0</SortType>"
            "<Ascending>0</Ascending>"
            f"<UnreadPreferred>{unread_pref}</UnreadPreferred>"
            "</request>"
        )
        result = self._live_module.sms_read_inbox(xml_body)
        if not result:
            raise RuntimeError("hilink_sms_read_empty_response")
        root = ET.fromstring(result)
        out: list[SmsMessage] = []
        for msg in root.findall(".//Message"):
            message_id = _xml_child_text(msg, "Index")
            sender = _xml_child_text(msg, "Phone")
            text = _xml_child_text(msg, "Content")
            timestamp = _xml_child_text(msg, "Date")
            read = _xml_child_text(msg, "Smstat") not in {"", "0"}
            out.append(
                SmsMessage(
                    message_id=str(message_id or "0"),
                    sender=str(sender or ""),
                    text=str(text or ""),
                    timestamp=str(timestamp or ""),
                    read=read,
                    protocol=self.protocol,
                    raw={
                        "index": message_id,
                        "smstat": _xml_child_text(msg, "Smstat"),
                    },
                )
            )
        return out

    def send_message(self, number: str, text: str) -> bool:
        content = str(text or "")[:160]
        xml_body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<request>"
            "<Index>-1</Index>"
            f"<Phones><Phone>{_xml_escape(number)}</Phone></Phones>"
            "<Sca></Sca>"
            f"<Content>{_xml_escape(content)}</Content>"
            f"<Length>{len(content)}</Length>"
            "<Reserved>1</Reserved>"
            f"<Date>{time.strftime('%Y-%m-%d %H:%M:%S')}</Date>"
            "</request>"
        )
        try:
            return bool(self._live_module.sms_send(xml_body))
        except Exception as exc:
            log.warning("HiLink SMS send failed: %s", exc)
            return False

    def delete_message(self, message_id: str) -> bool:
        xml_body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<request><Index>{_xml_escape(str(message_id or ''))}</Index></request>"
        )
        try:
            return bool(self._live_module.sms_delete(xml_body))
        except Exception as exc:
            log.warning("HiLink SMS delete failed: %s", exc)
            return False

SMS_SUPPORT_REMOVED_REASON = "SMS support is unavailable on this modem profile."
ROUTEROS_SMS_UNAVAILABLE_REASON = "MikroTik SMS is unavailable in this beta: full-storage recovery is not verified."
SMS_INBOX_MIN_POLL_INTERVAL_S = 121.0
SMS_COMMAND_MAX_AGE_S = 10.0 * 60.0
SMS_COMMAND_TERMINAL_STATES = frozenset(
    {
        "accepted",
        "confirmed",
        "settled",
        "failed",
        "rejected",
        "timed_out",
        "cancelled",
    }
)
SMS_COMMAND_DELETE_TERMINAL_STATES = frozenset(
    {"accepted", "confirmed", "settled"}
)
SMS_COMMAND_DEDUPE_LIMIT = 256
SMS_COMMAND_RECEIPTS_FILENAME = "sms_command_consumption.json"


def build_sms_transport(live_module: Any) -> SmsTransport:
    """Choose the safest canonical SMS transport for the attached modem."""
    if live_module is None:
        return NullSmsTransport("live_module_missing")

    cfg = getattr(live_module, "cfg", None)
    backend = str(getattr(cfg, "modem_backend", "") or "").strip().lower()
    owner_name = str(getattr(type(live_module), "__name__", "") or "").strip().lower()

    # RouterOS owns receiving. CGF cannot safely recover a full internal SMS
    # store on this beta; a generic wrapper is not evidence of SMS support.
    if backend in {"mikrotik", "chateau", "chateau_5g"} or "mikrotik" in owner_name:
        return NullSmsTransport(ROUTEROS_SMS_UNAVAILABLE_REASON)

    if backend == "nx520":
        # Matrix selected-profile admission remains owned by Modem Live.
        return ReceiveSmsTransport(live_module)

    hilink_hint = any(token in backend for token in ("b818", "b535", "huawei", "hilink"))
    if hilink_hint and all(callable(getattr(live_module, name, None)) for name in ("sms_read_inbox", "sms_send", "sms_delete")):
        return HilinkSmsTransport(live_module)

    # Explicit transport doubles/embedded adapters do not have a production
    # backend config.  Configured production runtimes must select a reviewed
    # transport without exposing or inspecting Modem Live's physical backend.
    if not backend and callable(getattr(live_module, "read_sms_messages", None)):
        return LiveModuleSmsTransport(live_module)

    return NullSmsTransport(SMS_SUPPORT_REMOVED_REASON)


def parse_command(text: str, *, code_word: str = "") -> Optional[SmsCommand]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw != raw.lower():
        return None
    keyword = str(code_word or "").strip()
    if keyword and raw == keyword:
        return SmsCommand.build("REBOOT", raw=raw)
    if raw == "reboot":
        return SmsCommand.build("REBOOT", raw=raw)

    pband = 0
    ca_pool: list[int] = []
    saw_band_command = False
    for token in raw.split():
        if re.fullmatch(r"p\d+", token):
            pband = int(token[1:])
            saw_band_command = True
            continue
        if re.fullmatch(r"c\d+(,\d+)*", token):
            ca_pool = _unique_bands(int(b) for b in token[1:].split(","))
            saw_band_command = True
            continue
        return None
    if not saw_band_command:
        return None
    if pband > 0:
        return SmsCommand.build(
            "PBAND",
            params={
                "pband": pband,
                "ca_pool": _unique_bands([pband, *ca_pool]),
            },
            raw=raw,
        )
    if ca_pool:
        return SmsCommand.build("BAND", params={"bands": ca_pool}, raw=raw)
    return None


def _unique_bands(raw_bands: Any) -> list[int]:
    out: list[int] = []
    for raw_band in raw_bands:
        try:
            band = int(raw_band or 0)
        except Exception:
            band = 0
        if band > 0 and band not in out:
            out.append(band)
    return out


class SmsInboxAuthority:
    """Sole owner of SMS acquisition cadence and latest-message truth."""

    def __init__(
        self,
        transport: SmsTransport,
        *,
        readiness_provider: Callable[[], bool],
        poll_interval_s: float = SMS_INBOX_MIN_POLL_INTERVAL_S,
        support_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._transport = transport
        if not callable(readiness_provider):
            raise TypeError("sms_identity_readiness_provider_required")
        self._readiness_provider = readiness_provider
        self._support_provider = support_provider
        self._poll_interval_s = _clamp_poll_interval(poll_interval_s)
        self._last_poll_ts = 0.0
        self._last_poll_now = 0.0
        self._latest_message_preview: dict[str, str] = {}
        self._runtime_events: SimpleQueue = SimpleQueue()
        self._last_status = self._surface(
            "unsupported" if isinstance(transport, NullSmsTransport) else "ready",
            (
                transport.reason
                if isinstance(transport, NullSmsTransport)
                else "No received SMS found yet."
            ),
        )

    @property
    def supported(self) -> bool:
        if isinstance(self._transport, NullSmsTransport):
            return False
        if self._support_provider is None:
            return True
        try:
            return bool(self._support_provider())
        except Exception as exc:
            log.warning("SMS capability read failed: %s", exc)
            return False

    @property
    def poll_interval_s(self) -> float:
        return float(self._poll_interval_s)

    def set_poll_interval(self, value: float) -> None:
        self._poll_interval_s = _clamp_poll_interval(value)
        # Cadence configuration is the only legal mutation here.  In
        # particular, command-control changes must not erase inbox fault,
        # message, timestamp, or unread truth owned by this authority.
        self._last_status = {
            **dict(self._last_status),
            "poll_interval_s": float(self._poll_interval_s),
        }

    def poll(self, *, now: float | None = None) -> SmsInboxPoll:
        ts = float(now if now is not None else time.time())
        if not self.supported:
            reason = str(
                getattr(
                    self._transport,
                    "reason",
                    "sms_not_supported_by_modem_live_matrix",
                )
            )
            self._last_status = self._surface("unsupported", reason, ts=ts)
            return SmsInboxPoll(polled=False)
        if not self._identity_ready():
            # Waiting does not consume the121s poll slot or erase private history.
            return SmsInboxPoll(polled=False)
        if ts - self._last_poll_ts < self._poll_interval_s:
            return SmsInboxPoll(polled=False)
        self._last_poll_ts = ts
        self._last_poll_now = ts
        try:
            messages = tuple(
                self._transport.read_messages(limit=20, unread_only=False) or ()
            )
        except Exception as exc:
            detail = f"SMS inbox read failed: {type(exc).__name__}: {exc}"
            previous = self._display_message(seen=1 if self._latest_message_preview else 0)
            message = f"{detail}. Last received: {previous}" if self._latest_message_preview else detail
            self._last_status = self._surface(
                "error",
                message,
                ts=ts,
                fault={
                    "operation": "read_messages",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            self._runtime_events.put(
                {
                    "kind": "sms_runtime",
                    "event": "inbox_read_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "continued": True,
                    "ts": ts,
                }
            )
            log.warning("SMS inbox read failed: %s", exc)
            return SmsInboxPoll(polled=True)
        if messages:
            self._latest_message_preview = _message_preview(messages[0])
        self._last_status = self._surface(
            "ready",
            self._display_message(seen=len(messages)),
            ts=ts,
            unread_count=sum(1 for message in messages if not bool(message.read)),
        )
        return SmsInboxPoll(polled=True, messages=messages)

    def delete_message(self, message_id: str) -> bool:
        if not self.supported or not str(message_id or ""):
            return False
        return bool(self._transport.delete_message(str(message_id)))

    def snapshot(self) -> dict[str, Any]:
        supported = bool(self.supported)
        if not self._identity_ready():
            return {
                **dict(self._last_status),
                "supported": supported,
                "status": "waiting",
                "message": "Waiting for router confirmation before reading SMS.",
                "latest_message": {},
                "unread_count": 0,
                "fault": dict(self._last_status.get("fault") or {}),
            }
        return {
            **dict(self._last_status),
            "supported": supported,
            "latest_message": dict(
                self._last_status.get("latest_message") or {}
            ),
            "fault": dict(self._last_status.get("fault") or {}),
        }

    def _identity_ready(self) -> bool:
        ready = self._readiness_provider()
        if type(ready) is not bool:
            raise TypeError("sms_identity_readiness_boolean_required")
        return ready

    def drain_runtime_events(self) -> list[dict[str, Any]]:
        return _drain_sms_events(self._runtime_events)

    def _surface(
        self,
        status: str,
        message: str,
        *,
        ts: float | None = None,
        unread_count: int = 0,
        fault: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": str(status or "unknown"),
            "message": str(message or ""),
            "protocol": str(
                getattr(self._transport, "protocol", "unknown") or "unknown"
            ),
            "supported": bool(self.supported),
            "unread_count": int(unread_count or 0),
            "latest_message": dict(self._latest_message_preview),
            "poll_interval_s": float(self._poll_interval_s),
            "last_poll_ts": float(self._last_poll_ts),
            "fault": dict(fault or {}),
            "source": "sms_inbox_authority",
            "ts": float(ts if ts is not None else time.time()),
        }

    def _display_message(self, *, seen: int) -> str:
        if self._latest_message_preview:
            latest = self._latest_message_preview
            sender = str(latest.get("sender") or "").strip()
            text = str(latest.get("text") or "").strip()
            timestamp = str(latest.get("timestamp") or "").strip()
            stamp = f" [{timestamp}]" if timestamp else " [no timestamp]"
            if text:
                prefix = f"{sender}: " if sender else ""
                return f"{prefix}{text}{stamp}".strip()
        if seen <= 0:
            return "No received SMS found yet."
        return "Latest received SMS has no displayable text."

    @staticmethod
    def disabled_surface(*, poll_interval_s: float, supported: bool) -> dict[str, Any]:
        supported_flag = bool(supported)
        return {
            "status": "disabled" if supported_flag else "unsupported",
            "message": "",
            "protocol": "disabled" if supported_flag else "unsupported",
            "supported": supported_flag,
            "unread_count": 0,
            "latest_message": {},
            "poll_interval_s": float(_clamp_poll_interval(poll_interval_s)),
            "last_poll_ts": 0.0,
            "fault": {},
            "source": "sms_inbox_authority",
            "ts": float(time.time()),
        }


class SmsCommandPolicyAuthority:
    """Sole owner of SMS command authorization and interpretation."""

    def __init__(
        self,
        *,
        enabled: bool,
        approved_senders: list[str] | None = None,
        code_word: str = "reboot",
        delete_processed: bool = False,
        receipt_path: Path | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._approved_senders = _normalize_numbers(approved_senders or [])
        self._code_word = str(code_word or "reboot").strip().lower()
        self._delete_processed = bool(delete_processed)
        self._last_command_count = 0
        self._last_rejected_count = 0
        self._last_ignored_count = 0
        # Foundation always supplies a persistent path. None is the explicit
        # in-memory boundary used by unit tests and embedded callers.
        self._receipt_path = Path(receipt_path) if receipt_path is not None else None
        self._receipt_lock = RLock()
        self._receipts_loaded = False
        self._consumed_commands: dict[str, float] = {}
        self._expired_before = 0.0
        self._runtime_events: SimpleQueue = SimpleQueue()

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    @property
    def delete_processed(self) -> bool:
        return bool(self._delete_processed)

    def configure(
        self,
        *,
        enabled: bool,
        approved_senders: list[str],
        code_word: str,
        delete_processed: bool,
    ) -> None:
        with self._receipt_lock:
            self._enabled = bool(enabled)
            self._approved_senders = _normalize_numbers(approved_senders)
            self._code_word = str(code_word or "reboot").strip().lower()
            self._delete_processed = bool(delete_processed)

    def evaluate(
        self,
        messages: tuple[SmsMessage, ...],
        *,
        now: float,
    ) -> tuple[SmsCommand, ...]:
        with self._receipt_lock:
            return self._evaluate_locked(messages, now=now)

    def _evaluate_locked(
        self, messages: tuple[SmsMessage, ...], *, now: float,
    ) -> tuple[SmsCommand, ...]:
        if not math.isfinite(now) or now <= 0:
            raise ValueError("sms_evaluation_time_invalid")
        self._load_receipts()
        commands: list[SmsCommand] = []
        next_receipts = dict(self._consumed_commands)
        rejected = 0
        ignored = 0
        for message in messages:
            parsed = parse_command(message.text, code_word=self._code_word)
            if parsed is None:
                ignored += 1
                continue
            if not self._enabled:
                ignored += 1
                continue
            received_ts = _parse_message_timestamp(message.timestamp)
            if (received_ts is None or not math.isfinite(received_ts)
                    or received_ts <= 0 or received_ts > now + 30.0):
                rejected += 1
                continue
            if (now - received_ts > SMS_COMMAND_MAX_AGE_S
                    or received_ts < self._expired_before):
                rejected += 1
                continue
            sender = _normalize_number(message.sender)
            if not self._sender_is_approved(sender):
                rejected += 1
                continue
            params = parsed.params
            if parsed.kind == "REBOOT":
                params["code_word"] = self._code_word
            command = SmsCommand.build(
                    parsed.kind,
                    params=params,
                    raw=parsed.raw,
                ).with_message(
                    message,
                    normalized_sender=sender,
                    received_ts=received_ts,
                )
            if command.command_id in next_receipts:
                ignored += 1
                self._runtime_events.put({
                    "kind": "sms_runtime", "event": "command_replay_suppressed",
                    "command_id": command.command_id,
                    "message_id": command.message_id,
                    "received_ts": command.received_ts, "ts": now,
                })
                continue
            next_receipts[command.command_id] = received_ts
            commands.append(command)
        if commands:
            expired_before = max(self._expired_before, now - SMS_COMMAND_MAX_AGE_S)
            next_receipts = {
                key: stamp for key, stamp in next_receipts.items()
                if stamp >= expired_before
            }
            if len(next_receipts) > SMS_COMMAND_DEDUPE_LIMIT:
                raise RuntimeError("sms_consumption_capacity_exhausted_no_commands_sent")
            # Save before returning anything executable. Write failure must not
            # fall back to RAM or allow an action with uncertain consumption.
            if self._receipt_path is not None:
                atomic_write_json(self._receipt_path, {
                    "schema": "SmsCommandConsumption.v1",
                    "expired_before": expired_before,
                    "commands": next_receipts,
                })
            self._consumed_commands = next_receipts
            self._expired_before = expired_before
            for command in commands:
                self._runtime_events.put({
                    "kind": "sms_runtime", "event": "command_consumed",
                    "command_id": command.command_id, "command": command.kind,
                    "message_id": command.message_id,
                    "received_ts": command.received_ts, "ts": now,
                    "persistent": self._receipt_path is not None,
                })
        self._last_command_count = len(commands)
        self._last_rejected_count = rejected
        self._last_ignored_count = ignored
        return tuple(commands)

    def _load_receipts(self) -> None:
        if self._receipts_loaded:
            return
        path = self._receipt_path
        if path is not None and path.exists():
            if path.stat().st_size > 64 * 1024:
                raise ValueError("sms_consumption_store_too_large")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict)
                    or set(data) != {"schema", "expired_before", "commands"}
                    or data["schema"] != "SmsCommandConsumption.v1"):
                raise ValueError("sms_consumption_store_invalid")
            before = data["expired_before"]
            rows = data["commands"]
            if (type(before) not in (int, float) or not math.isfinite(before)
                    or before < 0 or not isinstance(rows, dict)
                    or len(rows) > SMS_COMMAND_DEDUPE_LIMIT):
                raise ValueError("sms_consumption_store_invalid")
            for key, value in rows.items():
                if (re.fullmatch(r"sms:[0-9a-f]{64}", key) is None
                        or type(value) not in (int, float)
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError("sms_consumption_store_invalid")
            self._consumed_commands = dict(rows)
            self._expired_before = float(before)
        self._receipts_loaded = True

    def require_consumed(self, command: SmsCommand) -> None:
        with self._receipt_lock:
            self._load_receipts()
            if self._consumed_commands.get(command.command_id) != command.received_ts:
                raise RuntimeError("sms_unconsumed_command_submission_illegal")

    def record_command_submission(
        self,
        command: SmsCommand,
        *,
        submitted: bool,
        duplicate: bool,
        reason: str,
    ) -> None:
        self._runtime_events.put(
            {
                "kind": "sms_runtime",
                "event": "command_submission_recorded",
                "command": command.kind,
                "command_id": command.command_id,
                "submitted": bool(submitted),
                "duplicate": bool(duplicate),
                "reason": str(reason or ""),
                "terminal": False,
                "direct_modem_actuation": False,
                "routed_through_core_command_gateway": True,
                "ts": time.time(),
            }
        )

    def record_command_terminal(
        self,
        command: SmsCommand,
        *,
        state: str,
        reason: str,
        message_deleted: bool,
    ) -> None:
        lifecycle = str(state or "").strip().lower()
        if lifecycle not in SMS_COMMAND_TERMINAL_STATES:
            raise ValueError("sms_terminal_state_required")
        self._runtime_events.put(
            {
                "kind": "sms_runtime",
                "event": "command_terminal_recorded",
                "command": command.kind,
                "command_id": command.command_id,
                "terminal": True,
                "terminal_state": lifecycle,
                "reason": str(reason or lifecycle),
                "message_deleted": bool(message_deleted),
                "direct_modem_actuation": False,
                "routed_through_core_command_gateway": True,
                "ts": time.time(),
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self._enabled),
            "approved_sender_count": len(self._approved_senders),
            "accepts_any_sender": not bool(self._approved_senders),
            "code_word_configured": bool(self._code_word),
            "code_word": str(self._code_word or ""),
            "delete_processed": bool(self._delete_processed),
            "last_command_count": int(self._last_command_count),
            "rejected_count": int(self._last_rejected_count),
            "ignored_non_command_count": int(self._last_ignored_count),
            "handled_command_count": len(self._consumed_commands),
            "direct_modem_actuation": False,
            "routed_through_core_command_gateway": True,
            "source": "sms_command_policy_authority",
        }

    def drain_runtime_events(self) -> list[dict[str, Any]]:
        return _drain_sms_events(self._runtime_events)

    def _sender_is_approved(self, number: str) -> bool:
        if not self._approved_senders:
            return True
        return bool(number and number in self._approved_senders)


class SmsRuntimeBoundary:
    """Explicit boundary joining inbox policy to terminal Core receipts."""

    def __init__(
        self,
        inbox: SmsInboxAuthority,
        command_policy: SmsCommandPolicyAuthority,
    ) -> None:
        self._inbox = inbox
        self._command_policy = command_policy
        self._pending_commands: dict[str, SmsCommand] = {}

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        if str(getattr(config, "generation_id", "") or "") != str(
            generation_id or ""
        ):
            raise RuntimeError("sms_config_generation_id_mismatch")
        if str(getattr(config, "fingerprint", "") or "") != str(
            config_fingerprint or ""
        ):
            raise RuntimeError("sms_config_generation_fingerprint_mismatch")
        poll_interval_s = max(
            121.0,
            float(getattr(config, "sms_inbox_poll_interval_s", 121.0) or 121.0),
        )
        self._inbox.set_poll_interval(poll_interval_s)
        self._command_policy.configure(
            enabled=bool(getattr(config, "sms_command_enabled", False)),
            approved_senders=list(
                getattr(config, "sms_command_approved_senders", ()) or ()
            ),
            code_word=str(
                getattr(config, "sms_command_code_word", "reboot") or "reboot"
            ),
            delete_processed=bool(
                getattr(config, "sms_command_delete_processed", False)
            ),
        )
        self._config_generation_id = str(generation_id or "")
        self._config_fingerprint = str(config_fingerprint or "")

    def poll(self, *, now: float | None = None) -> tuple[SmsCommand, ...]:
        ts = float(now if now is not None else time.time())
        result = self._inbox.poll(now=ts)
        if not result.polled or not self._command_policy.enabled:
            return ()
        return self._command_policy.evaluate(result.messages, now=ts)

    def record_command_submission(
        self,
        command: SmsCommand,
        *,
        submitted: bool,
        duplicate: bool,
        reason: str,
    ) -> None:
        if not isinstance(command, SmsCommand) or not command.command_id:
            raise TypeError("sms_command_submission_requires_typed_command")
        if submitted:
            self._pending_commands[command.command_id] = command
        self._command_policy.record_command_submission(
            command,
            submitted=bool(submitted),
            duplicate=duplicate,
            reason=reason,
        )

    def require_consumed(self, command: SmsCommand) -> None:
        self._command_policy.require_consumed(command)

    def observe_command_terminal(
        self,
        *,
        command_id: str,
        state: str,
        reason: str,
    ) -> bool:
        identity = str(command_id or "").strip()
        lifecycle = str(state or "").strip().lower()
        if not identity:
            raise ValueError("sms_terminal_command_identity_required")
        if lifecycle not in SMS_COMMAND_TERMINAL_STATES:
            raise ValueError("sms_terminal_state_required")
        command = self._pending_commands.pop(identity, None)
        if command is None:
            return False
        deleted = False
        if (
            lifecycle in SMS_COMMAND_DELETE_TERMINAL_STATES
            and self._command_policy.delete_processed
            and command.message_id
        ):
            deleted = self._inbox.delete_message(command.message_id)
        self._command_policy.record_command_terminal(
            command,
            state=lifecycle,
            reason=str(reason or lifecycle),
            message_deleted=deleted,
        )
        return True

    def acknowledge_command_lifecycle(self, *args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        raise RuntimeError(
            "sms_submission_time_acknowledgement_deleted_use_terminal_outcome"
        )

    def snapshot(self) -> dict[str, Any]:
        command_control = self._command_policy.snapshot()
        inbox = self._inbox.snapshot()
        command_control["enabled"] = bool(
            command_control["enabled"] and inbox["supported"]
        )
        return {
            "schema_version": "sms_runtime_v2",
            "inbox": inbox,
            "command_control": command_control,
            "pending_command_count": len(self._pending_commands),
            "source": "sms_runtime_boundary",
            "direct_modem_actuation": False,
            "routed_through_core_command_gateway": True,
            "config_generation_id": str(
                getattr(self, "_config_generation_id", "") or ""
            ),
            "config_fingerprint": str(
                getattr(self, "_config_fingerprint", "") or ""
            ),
        }

    def drain_runtime_events(self) -> list[dict[str, Any]]:
        return [
            *self._inbox.drain_runtime_events(),
            *self._command_policy.drain_runtime_events(),
        ]


def _coerce_message(raw: Any, *, protocol: str) -> SmsMessage:
    if isinstance(raw, SmsMessage):
        return raw
    data = raw if isinstance(raw, dict) else {}
    message_id = str(
        data.get("message_id")
        or data.get("id")
        or data.get("index")
        or data.get("idx")
        or ""
    )
    text = str(data.get("text") or data.get("content") or data.get("content_decoded") or "")
    sender = str(data.get("sender") or data.get("number") or data.get("phone") or "")
    return SmsMessage(
        message_id=message_id,
        sender=sender,
        text=text,
        timestamp=str(data.get("timestamp") or data.get("date") or ""),
        read=safe_bool(data.get("read", False), False),
        protocol=str(data.get("protocol") or protocol),
        raw=dict(data),
    )


def _message_preview(message: SmsMessage) -> dict[str, str]:
    text = " ".join(str(message.text or "").split())
    if len(text) > 80:
        text = f"{text[:77]}..."
    timestamp = str(message.timestamp or "")
    preview = {
        "sender": str(message.sender or ""),
        "timestamp": timestamp,
        "text": text,
    }
    received_ts = _parse_message_timestamp(timestamp)
    if received_ts is not None:
        preview["received_ts"] = str(round(received_ts, 3))
    return preview


def _parse_message_timestamp(value: str) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            numeric = float(raw)
        except Exception:
            return None
        if numeric > 1_000_000_000_000:
            numeric = numeric / 1000.0
        return numeric
    candidates = [raw]
    if "T" in raw:
        candidates.append(raw.replace("Z", "+00:00"))
    else:
        candidates.extend([
            raw.replace("/", "-"),
            raw.replace(".", "-"),
        ])
    for candidate in candidates:
        try:
            dt = _dt.datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
            return dt.timestamp()
        except Exception:
            pass
    for candidate, fmt in (
        (raw, "%Y-%m-%d %H:%M:%S"),
        (raw, "%Y-%m-%d %H:%M"),
        (raw, "%y/%m/%d,%H:%M:%S%z"),
        (raw, "%y/%m/%d,%H:%M:%S"),
        (raw.title(), "%b/%d/%Y %H:%M:%S"),
    ):
        try:
            dt = _dt.datetime.strptime(candidate, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
            return dt.timestamp()
        except Exception:
            pass
    return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _xml_child_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return str(child.text or "").strip() if child is not None else ""


def _normalize_number(phone: str) -> str:
    value = re.sub(r"[\s\-\(\)]", "", str(phone or "").strip())
    if value.startswith("00") and len(value) > 4 and value.isdigit():
        return "+" + value[2:]
    return value


def _normalize_numbers(phones: list[str]) -> set[str]:
    return {normalized for item in phones if (normalized := _normalize_number(item))}


def _clamp_poll_interval(value: float | None) -> float:
    try:
        raw = float(value or SMS_INBOX_MIN_POLL_INTERVAL_S)
    except Exception:
        raw = SMS_INBOX_MIN_POLL_INTERVAL_S
    return max(SMS_INBOX_MIN_POLL_INTERVAL_S, raw)


def _xml_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
