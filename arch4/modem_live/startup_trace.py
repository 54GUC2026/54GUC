"""Bounded application-protocol evidence; never a modem or storage authority."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from http.client import IncompleteRead
import json
import hashlib
import math
import re
import threading
import time
import uuid
import requests
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from arch4.privacy_scrub import SCRUB_MASK, scrub_record, private_field_alias_scope, native_payload_redaction_required

TRACE_FILENAME = "modem_startup_trace.jsonl"
TRACE_SCHEMA = "ModemStartupWireTrace.v1"
TRACE_OWNER = "modem_live.startup_trace"
TRACE_MAX_BYTES = 8 * 1024 * 1024
BODY_MAX_BYTES = 128 * 1024
_CONTEXT: ContextVar[Any] = ContextVar("modem_startup_trace", default=None)


def _safe_value(value: Any) -> Any:
    def convert(item: Any) -> Any:
        if isinstance(item, bytes):
            if len(item) > BODY_MAX_BYTES:
                return {"omitted": "body_size_limit", "size_bytes": len(item)}
            try:
                text = item.decode("utf-8", errors="strict")
            except UnicodeError:
                return {"omitted": "opaque_bytes", "size_bytes": len(item)}
            if any(not char.isprintable() and char not in "\t\r\n" for char in text):
                return {"omitted": "opaque_bytes", "size_bytes": len(item)}
            return text
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                key = convert(key) if isinstance(key, bytes) else key
                if not isinstance(key, str):
                    raise ValueError("modem_trace_mapping_key_not_text")
                if key in result:
                    raise ValueError("modem_trace_mapping_key_collision")
                result[key] = convert(child)
            # ubus credentials occupy a positional slot, not a sensitive key.
            if result.get("method") == "call" and isinstance(result.get("params"), list) and result["params"]:
                result["params"][0] = SCRUB_MASK
            return result
        if isinstance(item, (list, tuple)):
            return [convert(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return {"omitted": "opaque_object", "type": type(item).__name__}

    # Keep typed context intact for the sole privacy owner (including public APN).
    return scrub_record({"value": convert(value)})["value"]


def _body(value: Any, content_type: str = "") -> Any:
    if native_payload_redaction_required():
        return {"omitted": "profile_private_source_not_projectable"}
    def bounded(body: Any) -> Any:
        clean = _safe_value(body)
        size = len(json.dumps(clean, ensure_ascii=True, allow_nan=False).encode("utf-8"))
        if size > BODY_MAX_BYTES:
            return {"omitted": "body_size_limit", "size_bytes": size}
        return clean

    if isinstance(value, (dict, list, tuple)):
        return bounded(value)
    if value is None:
        return None
    if not isinstance(value, (str, bytes)):
        return {"omitted": "opaque_body"}
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > BODY_MAX_BYTES:
        return {"omitted": "body_size_limit", "size_bytes": len(raw)}
    try:
        return bounded(json.loads(raw))
    except (ValueError, UnicodeError):
        pass
    try:
        text = raw.decode("utf-8")
        if "xml" in content_type.lower() or text.lstrip().startswith("<?xml"):
            # Never expand DTD/entity declarations, including local content.
            if "<!" in text:
                raise ValueError("xml_declaration")
            root = ElementTree.fromstring(text)
            def convert(node: Any) -> Any:
                return {"tag": node.tag, "attributes": _safe_value(node.attrib),
                        "value": scrub_record({node.tag: node.text or ""})[node.tag],
                        "children": [convert(child) for child in node]}
            return bounded(convert(root))
        if "application/x-www-form-urlencoded" in content_type.lower():
            return bounded(dict(parse_qsl(text, keep_blank_values=True)))
    except (ValueError, UnicodeError, ElementTree.ParseError):
        pass
    return {"omitted": "unstructured_body", "size_bytes": len(raw)}


def native_reply_hash(value):
    """Bind a conversion view without copying or hashing private raw data."""
    content_type = "application/xml" if isinstance(value, str) and value.lstrip().startswith('<') else ""
    clean = _body(value, content_type)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _headers(headers: Any) -> dict[str, Any]:
    # Unknown headers can contain credentials. Keep names, allow only public values.
    public = {"content-type", "content-length", "z-mode", "z-tag", "x-requested-with",
              "connection", "accept", "user-agent", "server", "date", "cache-control"}
    if not hasattr(headers, "items"):
        return {}
    return {str(k): str(v) if str(k).lower() in public else SCRUB_MASK
            for k, v in headers.items()}


def _url(url: Any) -> dict[str, Any]:
    parts = urlsplit(str(url))
    path = re.sub(r"/;stok=[^/;?]+", "", parts.path)
    return {"scheme": parts.scheme, "path": path, "session_path_masked": path != parts.path,
            "query": _safe_value(dict(parse_qsl(parts.query, keep_blank_values=True)))}


def _http_failure_details(error: Exception, response: Any) -> dict[str, Any]:
    """Bounded scalar snapshot only: never error text, body, cookies or I/O."""
    details: dict[str, Any] = {"response_headers_received": response is not None, "chain": []}
    pending, seen = [(error, None, "raised")], set()
    while pending and len(details["chain"]) < 6:
        current, parent, relation = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        item = {"type": type(current).__name__[:80], "via": relation}
        if parent is not None:
            item["parent_index"] = parent
        if isinstance(current, IncompleteRead):
            partial = current.partial
            count = len(partial) if isinstance(partial, bytes) else partial
            for key, value in (("received_bytes", count), ("remaining_bytes", current.expected)):
                if type(value) is int and 0 <= value < 2**63:
                    item[key] = value
        if isinstance(current, OSError):
            for key in ("errno", "winerror"):
                value = getattr(current, key, None)
                if type(value) is int:
                    item[key] = value
        index = len(details["chain"])
        details["chain"].append(item)
        links = (("cause", current.__cause__), ("context", current.__context__),
                 *(("argument", value) for value in current.args[:4]))
        pending.extend((value, index, via) for via, value in links if isinstance(value, BaseException))
    if response is not None:
        headers = {}
        public_tokens = {
            "transfer-encoding": {"chunked", "gzip", "deflate", "compress", "identity"},
            "content-encoding": {"gzip", "deflate", "br", "zstd", "compress", "identity"},
            "connection": {"close", "keep-alive"},
            "content-type": {"application/json", "text/html", "text/plain", "application/xml", "text/xml"},
        }
        for name in ("content-length", *public_tokens):
            value = response.headers.get(name)
            if not isinstance(value, str):
                continue
            if name == "content-length":
                headers[name] = int(value) if len(value) <= 18 and value.isascii() and value.isdecimal() else "invalid"
            else:
                tokens = value.lower().split(";", 1)[0].replace(" ", "").split(",") if len(value) <= 96 else []
                headers[name] = ",".join(tokens) if tokens and set(tokens) <= public_tokens[name] else "unrecognized"
        captured = {"status": response.status_code, "headers": headers}
        raw = response.raw
        # urllib3 exposes counters already held in memory; no content is read.
        if raw is not None:
            for key, value in (("http_version", getattr(raw, "version", None)),
                               ("body_bytes_remaining", getattr(raw, "length_remaining", None))):
                if type(value) is int and 0 <= value < 2**63:
                    captured[key] = value
            try:
                count = raw.tell()
                if type(count) is int and 0 <= count < 2**63:
                    captured["body_bytes_received"] = count
            except Exception:
                captured["body_progress_unavailable"] = True
        details["response"] = captured
    return details


class StartupModemTrace:
    """One window across startup retries, supplied by Product composition."""
    def __init__(self, *, selected_at: float | None = None, monotonic=time.monotonic, wall_clock=time.time) -> None:
        self._clock = monotonic
        self._wall = wall_clock
        self._lock = threading.RLock()
        if selected_at is not None and (not math.isfinite(selected_at) or selected_at < 0 or selected_at > monotonic()):
            raise ValueError("modem_trace_selection_time_invalid")
        self._selected_at = selected_at
        self._started: float | None = None
        self._announced = False
        self._id = uuid.uuid4().hex
        self._sequence = 0
        self._requests = 0
        self._bytes = 0
        self._closed = False
        self.delivery_failures = 0

    def _emit(self, sink: Any, kind: str, **data: Any) -> None:
        # Diagnostics must not replace the transport's return value or exception.
        try:
            self._sequence += 1
            record = {"schema": TRACE_SCHEMA, "owner": TRACE_OWNER, "trace_id": self._id,
                      "sequence": self._sequence, "ts": self._wall(), "kind": kind, **data}
            size = len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode())
            if self._bytes + size > TRACE_MAX_BYTES - 4096:
                record = {**record, "kind": "trace_limit", "detail": "byte_budget_exhausted"}
                for key in data:
                    record.pop(key, None)
                self._closed = True
                size = len(json.dumps(record).encode())
            self._bytes += size
            if not sink(record):
                self.delivery_failures += 1
        except Exception:
            self.delivery_failures += 1

    def begin(self, sink: Any, lane: str, operation: str, request: Any, *, operation_id=None) -> int | None:
        with self._lock:
            now = self._clock()
            if self._started is None:
                self._started = now
            if not self._announced:
                self._announced = True
                self._emit(sink, "trace_started", duration_s=60, trigger="first_modem_io",
                           trigger_ts=self._wall(),
                           selection_to_first_request_s=(round(now - self._selected_at, 6)
                                                        if self._selected_at is not None else 0.0))
            if self._closed:
                return None
            if now - self._started >= 60 or self._requests >= 1024:
                self._emit(sink, "trace_finished", reason=("deadline" if now - self._started >= 60 else "request_limit"))
                self._closed = True
                return None
            self._requests += 1
            request_id = self._requests
            self._emit(sink, "request", request_id=request_id, lane=lane,
                       operation=operation, operation_id=operation_id,
                       request=_safe_value(request() if callable(request) else request))
            return request_id

    def finish(self, sink: Any, request_id: int, **data: Any) -> None:
        with self._lock:
            if self._bytes < TRACE_MAX_BYTES - 4096:
                self._emit(sink, "response", request_id=request_id, **data)

    @contextmanager
    def scope(self, sink: Any, lane: str, operation: str, *, private_keys=(), opaque_native=False):
        token = _CONTEXT.set((self, sink, lane, operation, uuid.uuid4().hex))
        try:
            with private_field_alias_scope(private_keys, omit_native_payload=opaque_native):
                yield
        finally:
            _CONTEXT.reset(token)


@contextmanager
def trace_exchange(request: Any, *, optional_metadata=None):
    from arch4.observation import observation_capture_enabled, observation_native_detail_enabled, capture_native
    context = _CONTEXT.get()
    optional = observation_capture_enabled()
    started = time.monotonic()
    started_wall = time.time() if optional else 0.
    request_id = None
    lane = operation = operation_id = ''
    response: dict[str, Any] = {}
    if context is not None:
        trace, sink, lane, operation, operation_id = context
        try:
            request_id = trace.begin(sink, lane, operation, request, operation_id=operation_id)
        except Exception:
            trace.delivery_failures += 1
    if optional and request_id is None and not observation_native_detail_enabled():
        response['_optional_metadata_only'] = True
    try:
        yield response if request_id is not None or optional else None
    except BaseException as exc:
        details = response.get("error_details")
        response.clear()
        response["error_type"] = type(exc).__name__
        if details is not None:
            response["error_details"] = details
        raise
    finally:
        if optional:
            try:
                response.pop('_optional_metadata_only', None)
                capture_native(dict(kind='native_reply', lane=lane, operation=operation,
                    operation_id=operation_id, elapsed_s=round(time.monotonic()-started, 6),
                    request_started_at=started_wall, request_finished_at=time.time(),
                    native=(optional_metadata() if callable(optional_metadata) else optional_metadata or {}),
                    **_safe_value(response)))
            except Exception:
                pass  # An optional observer cannot replace a native result.
        if request_id is not None:
            try:
                trace.finish(sink, request_id, elapsed_s=round(time.monotonic() - started, 6),
                             operation_id=operation_id, **_safe_value(response))
            except Exception:
                trace.delivery_failures += 1


def trace_profile_decode(definition, replies, context, values, issues):
    """Conversion evidence in the existing native window; never opens a window.

    Raw replies are already captured at the transport boundary. Do not copy
    them here, or treat a cached conversion as proof of a new physical read.
    """
    from arch4.observation import observation_native_detail_enabled, capture_native
    if observation_native_detail_enabled():
        capture_native(dict(kind='profile_decode', profile=definition.provenance(),
            native=replies, canonical=values,
            issues=[dict(address=i.address, pointer=i.pointer, reason=i.reason) for i in issues]))
    active = _CONTEXT.get()
    if active is None:
        return
    trace, sink, lane, operation, operation_id = active
    try:
        with trace._lock:
            if trace._started is None or trace._closed or trace._clock() - trace._started >= 60:
                return
            planes = {}
            for address, value in values.items():
                line, field = address.split('.', 1)
                planes.setdefault(line, {})[field] = value
            trace._emit(sink, "profile_decode", operation_id=operation_id,
                        lane=lane, operation=operation, profile=definition.provenance(),
                        native_groups=sorted(replies),
                        reply_hashes={name: native_reply_hash(value) for name, value in replies.items()},
                        context=_body(context or {}),
                        values=_body(planes), issues=_body([
                            {"address": issue.address, "pointer": issue.pointer, "reason": issue.reason}
                            for issue in issues]))
    except Exception:
        trace.delivery_failures += 1


def traced_http(fn: Any, url: str, **kwargs: Any) -> Any:
    """Permanent HTTP boundary adapter, inside the existing pacing scope."""
    if _CONTEXT.get() is None:
        return fn(url, **kwargs)
    def request():
        try:
            headers = kwargs.get("headers", {})
            return {"protocol": "http", "method": getattr(fn, "__name__", "http"),
                    **_url(url), "headers": _headers(headers),
                    "query_arguments": _safe_value(kwargs.get("params")),
                    "timeout": _safe_value(kwargs.get("timeout")),
                    "tls_verification": ("device_certificate" if kwargs.get("verify") is False
                                         else "verified" if "verify" in kwargs else "session_default"),
                    "automatic_redirects": kwargs.get("allow_redirects", "session_default"),
                    "body": _body(kwargs.get("json", kwargs.get("data")),
                                  str(headers.get("Content-Type", "application/x-www-form-urlencoded")))}
        except Exception:
            return {"protocol": "http", "omitted": "request_capture_error"}
    with trace_exchange(request, optional_metadata=lambda: dict(protocol='http', method=getattr(fn, '__name__', 'http'), **_url(url))) as captured:
        if captured is None:
            return fn(url, **kwargs)
        received = [None]
        session = getattr(fn, "__self__", None)
        if isinstance(session, requests.Session):
            # Observe headers before requests consumes the body. Per-call hooks
            # preserve the caller/session hooks without mutating shared state.
            def remember_response(response, **unused):
                received[0] = response
            hooks = dict(requests.sessions.merge_hooks(
                requests.Request(hooks=kwargs.get("hooks")).hooks, session.hooks))
            previous = hooks.get("response") or ()
            callbacks = [previous] if callable(previous) else [hook for hook in previous if callable(hook)]
            hooks["response"] = [remember_response, *callbacks]
            kwargs["hooks"] = hooks
        try:
            result = fn(url, **kwargs)
        except Exception as exc:
            try:
                captured["error_details"] = _http_failure_details(exc, received[0])
            except Exception:
                captured["error_details"] = {"omitted": "capture_error"}
            raise
        finally:
            # PreparedRequest retains hooks. Do not let its observer retain
            # a Response/body through a reference cycle after this call.
            received[0] = None
        if captured.get('_optional_metadata_only'):
            captured['status'] = result.status_code
            return result
        try:
            replies = []
            history = result.history if isinstance(result.history, list) else []
            for reply in [*history, result]:
                prepared = getattr(reply, "request", None)
                replies.append({"status": reply.status_code,
                                "headers": _headers(reply.headers),
                                "body": _body(reply.content, str(reply.headers.get("Content-Type", ""))),
                                "sent_headers": _headers(getattr(prepared, "headers", {})),
                                "sent_url": _url(getattr(prepared, "url", url) or url)})
            captured["replies"] = replies
        except Exception:
            captured["omitted"] = "response_capture_error"
        return result


def traced_protocol(fn: Any, *, operation: str, args=(), kwargs=None, protocol: str = "routeros") -> Any:
    kwargs = kwargs or {}
    with trace_exchange(lambda: {"protocol": protocol, "operation": operation,
                                "args": _safe_value(args), "arguments": _safe_value(kwargs)},
                        optional_metadata=lambda: dict(protocol=protocol, operation=operation)) as captured:
        result = fn(*args, **kwargs)
        if captured is not None and not captured.get('_optional_metadata_only'):
            try:
                captured["result"] = _body(result)
            except Exception:
                captured["omitted"] = "response_capture_error"
        return result
