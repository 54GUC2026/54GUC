"""Frozen observation policy and bounded ports; no storage or modem ownership."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
import hashlib
from itertools import islice
import json
import re
from threading import RLock
import time
import uuid

SCHEMA = '54GUC.ObservationSettings.v1'
EVIDENCE_SCHEMA = 'ObservationEvidence.v1'
EVIDENCE_OWNER = 'cgf.observation'
EVIDENCE_FILENAME = 'observation_evidence.jsonl'
SUMMARY_FILENAME = 'observation_session.json'
CATEGORIES = frozenset({'modem_io', 'matrix_delivery', 'scout', 'brain', 'history'})
_SESSION = ContextVar('observation_session', default=None)
_CAPTURE = ContextVar('observation_capture', default=None)


@dataclass(frozen=True)
class RecordingPolicy:
    optional_categories: tuple[str, ...] = ()
    detail: str = 'normal'
    investigation_minutes: int = 0
    investigation_id: str = ''
    expires_at: float = 0.
    max_memory_records: int = 256
    max_records: int = 10000
    max_storage_bytes: int = 16 * 1024 * 1024
    retention_s: int = 86400


@dataclass(frozen=True)
class DiscoveryPolicy:
    related_models: str = 'reject'
    capture_unmapped_fields: bool = False
    extra_reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservationScope:
    profiles: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    firmware: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservationPolicy:
    recording: RecordingPolicy = RecordingPolicy()
    discovery: DiscoveryPolicy = DiscoveryPolicy()
    scope: ObservationScope = ObservationScope()

    def as_dict(self):
        return json.loads(json.dumps(asdict(self)))

    @property
    def revision(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_settings(cls, supplied):
        if not isinstance(supplied, dict):
            raise ValueError('observation_settings_mapping_required')
        sections = {}
        for name, kind in (('recording', RecordingPolicy), ('discovery', DiscoveryPolicy), ('scope', ObservationScope)):
            raw = supplied.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError('observation_section_mapping_required:' + name)
            values = {}
            for field in fields(kind):
                value = raw.get(field.name, field.default)
                if isinstance(field.default, tuple):
                    if name == 'discovery' and field.name == 'extra_reads' and value == 'disabled':
                        value = ()
                    if not isinstance(value, (tuple, list)) or len(value) > 64 or any(
                            type(v) is not str or not re.fullmatch(r'[\w. :/-]{1,128}', v, re.ASCII) for v in value):
                        raise ValueError('observation_list_invalid:' + field.name)
                    value = tuple(dict.fromkeys(value))
                elif type(field.default) is bool:
                    if type(value) is not bool:
                        raise ValueError('observation_boolean_required:' + field.name)
                elif type(field.default) is int:
                    limits = {'investigation_minutes': (0, 1440), 'max_memory_records': (8, 4096),
                              'max_records': (8, 100000), 'max_storage_bytes': (4096, 256*1024*1024),
                              'retention_s': (60, 7*86400)}
                    low, high = limits[field.name]
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError('observation_limit_invalid:' + field.name)
                elif field.name == 'expires_at':
                    if type(value) not in (float, int) or not 0 <= value < 1e12:
                        raise ValueError('observation_expiry_invalid')
                    value = float(value)
                elif type(value) is not str or len(value) > 128:
                    raise ValueError('observation_text_invalid:' + field.name)
                values[field.name] = value
            sections[name] = kind(**values)
        result = cls(**sections)
        if (result.recording.detail not in {'normal', 'extended'}
                or result.discovery.related_models not in {'reject', 'observe'}
                or not set(result.recording.optional_categories) <= CATEGORIES
                or any('://' in p for p in result.discovery.extra_reads)):
            raise ValueError('observation_choice_invalid')
        if result.recording.detail == 'extended' and result.recording.investigation_minutes == 0:
            raise ValueError('observation_extended_deadline_required')
        return result


def detached(value, *, depth=0, budget=None, limits=None):
    """At most128 leaves/8 levels/256 characters per scalar on a producer."""
    budget = [128] if budget is None else budget
    limits = [] if limits is None else limits
    if depth > 7 or budget[0] <= 0:
        limits.append(1)
        return {'omitted': 'capture_bound'}
    budget[0] -= 1
    if isinstance(value, dict):
        result = {str(k)[:128]: detached(v, depth=depth+1, budget=budget, limits=limits)
                  for k, v in islice(value.items(), 64) if budget[0] > 0}
        if len(result) < len(value):
            limits.append(len(value)-len(result))
        if depth == 0 and limits:
            previous = result.pop('_capture_limits', {})
            prior_count = previous.get('omitted_values_at_least', 0) if isinstance(previous, dict) else 1
            result = {'_capture_limits': {'omitted_values_at_least': prior_count + sum(limits)}, **result}
        return result
    if isinstance(value, (list, tuple)):
        result = [detached(v, depth=depth+1, budget=budget, limits=limits) for v in value[:32] if budget[0] > 0]
        if len(result) < len(value):
            limits.append(len(value)-len(result))
        return result
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if -1e100 < value < 1e100 else None
    if isinstance(value, bytes):
        limits.append(1)
        return {'omitted': 'opaque_bytes', 'size_bytes': len(value)}
    text = str(value)
    if len(text) > 256:
        limits.append(1)
    return text[:256]


class ObservationSession:
    """Per-composition observation state, injected into existing owners."""
    def __init__(self, policy=ObservationPolicy(), *, sink=None, wall_clock=time.time,
                 profile_id='', family=''):
        self.policy, self.sink, self.clock = policy, sink, wall_clock
        self.settings_revision = policy.revision
        self.profile_id, self.family = profile_id, family
        self.session_id = uuid.uuid4().hex
        self.source_epoch = ''
        self.import_status = {}
        self.modules = {'cgf': True, 'scout': False, 'brain': False, 'history': False}
        self._lock = RLock()
        self.read_only = False
        self.ready = False
        self.closed = False
        self._expired = False
        self.identity = {}
        self.scout_sink = None
        self.capture_stopped = ''
        self._counts = {'submitted': 0, 'omitted': 0, 'truncated_records': 0, 'sink_errors': 0}
        self._unapplied = set()

    def in_scope(self, identity=None):
        scope = self.policy.scope
        identity = self.identity if identity is None else identity
        return ((not scope.profiles or self.profile_id in scope.profiles)
                and (not scope.families or self.family in scope.families)
                and (not scope.firmware or str(identity.get('firmware', '')) in scope.firmware))

    def enabled(self, category):
        r = self.policy.recording
        return (not self.closed and not self.capture_stopped and self.in_scope()
                and (category in r.optional_categories or
                     category == 'scout' and self.policy.discovery.capture_unmapped_fields)
                and not self.expired())

    def expired(self):
        if self.policy.recording.expires_at and self.clock() >= self.policy.recording.expires_at:
            self._expired = True
        return self._expired

    def discovering(self):
        return (self.ready and not self.closed and not self.capture_stopped and self.in_scope()
                and not self.expired()
                and self.policy.discovery.capture_unmapped_fields)

    def admit_related(self, identity, reason):
        if self.policy.discovery.related_models != 'observe' or not self.in_scope(identity):
            return False
        identity = {k: str(identity.get(k) or '') for k in ('model', 'firmware', 'serial', 'imei')}
        with self._lock:
            if self.identity and any(self.identity.get(k) and identity[k]
                    and self.identity[k].casefold() != identity[k].casefold() for k in ('serial', 'imei')):
                return False
            self.identity.update({k: v for k, v in identity.items() if v})
            self.read_only = True
        return True

    def observe_identity(self, identity):
        """Recording scope uses facts already admitted by the device owner."""
        with self._lock:
            self.identity.update({k: str(identity[k]) for k in ('model', 'firmware', 'serial', 'imei')
                                  if identity.get(k) not in (None, '')})

    def emit(self, category, payload):
        if not self.enabled(category):
            return False
        with self._lock:
            self._counts['submitted'] += 1
        try:
            clean = detached(payload)
            if '_capture_limits' in clean:
                with self._lock:
                    self._counts['truncated_records'] += 1
            accepted = callable(self.sink) and self.sink(category, clean)
            if not accepted:
                with self._lock:
                    self._counts['omitted'] += 1
            return bool(accepted)
        except Exception:
            with self._lock:
                self._counts['sink_errors'] += 1
            return False

    @contextmanager
    def operation(self, lane, operation):
        token = _SESSION.set(self)
        pending = []
        capture = _CAPTURE.set(pending)
        try:
            if self.read_only and lane in {'event_write', 'recovery'}:
                raise PermissionError('observation_read_only')
            yield
        finally:
            _CAPTURE.reset(capture)
            _SESSION.reset(token)
            # This scope surrounds the existing I/O gate: its lock has now left.
            for row in pending:
                self.emit('modem_io', row)
                if self.discovering() and callable(self.scout_sink):
                    try:
                        result = self.scout_sink(row)
                        if isinstance(result, dict) and result.get('status') != 'queued':
                            self._counts['omitted'] += 1
                    except Exception:
                        self._counts['sink_errors'] += 1

    def summary(self):
        with self._lock:
            return dict(capture_id=self.session_id, settings_revision=self.settings_revision,
                        source_epoch=self.source_epoch, import_status=self.import_status,
                        modules=dict(self.modules),
                        unavailable_categories=[name for name in ('brain', 'history')
                            if name in self.policy.recording.optional_categories and not self.modules[name]],
                        profile_id=self.profile_id, related_observe_only=self.read_only,
                        capture_stopped=self.capture_stopped, authenticated_ready=self.ready,
                        expired=self.expired(),
                        **self._counts, unapplied=sorted(self._unapplied))

    def note_unapplied(self, *names):
        with self._lock:
            self._unapplied.update(str(name)[:160] for name in names[:128])


def active_observation():
    return _SESSION.get()


def allow_related_model(identity, reason):
    session = active_observation()
    return session is not None and session.admit_related(identity, reason)


def observation_capture_enabled():
    session = active_observation()
    return bool(session is not None and session.ready and
                (session.enabled('modem_io') or session.discovering()))


def observation_native_detail_enabled():
    session = active_observation()
    return bool(session is not None and session.ready and (session.discovering() or
        session.policy.recording.detail == 'extended' and session.enabled('modem_io')))


def capture_native(record):
    pending = _CAPTURE.get()
    session = active_observation()
    if pending is not None and observation_capture_enabled():
        if len(pending) < 16:
            pending.append(detached(record))
        else:
            session._counts['omitted'] += 1


def require_observation_read_only(lane, operation):
    session = active_observation()
    if session is not None and session.read_only:
        # SMS read and delete share a lane; classify the existing typed operation.
        native_write = bool(re.search(r'(?:^|[:/ _])(set|add|remove|enable|disable|reboot|restart|write|delete|run|at-chat)(?:$|[:/ _])', operation.lower()))
        if native_write or lane in {'event_write', 'recovery'} or (lane == 'sms' and
                any(w in operation.lower() for w in ('send', 'delete', 'write', 'set'))):
            raise PermissionError('observation_read_only')
