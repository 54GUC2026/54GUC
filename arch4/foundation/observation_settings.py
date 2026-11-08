"""Local observation settings exchange; one accepted authority, atomic snapshots."""
from dataclasses import dataclass
import json
from pathlib import Path
import time

from arch4.atomic_io import atomic_write_text
from arch4.observation import SCHEMA, ObservationPolicy

IMPORT_FILENAME = 'observation_settings.import.json'
EXPORT_FILENAME = 'observation_settings.export.json'
ACCEPTED_FILENAME = 'observation_settings.json'


@dataclass(frozen=True)
class ObservationImportReceipt:
    policy: ObservationPolicy
    changed: tuple[str, ...] = ()
    unapplied: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ObservationSettingsStore:
    def __init__(self, directory, *, wall_clock=time.time):
        self.directory = Path(directory)
        self.path = self.directory / ACCEPTED_FILENAME
        self.import_path = self.directory / IMPORT_FILENAME
        self.export_path = self.directory / EXPORT_FILENAME
        self.clock = wall_clock

    @staticmethod
    def _read(path):
        if path.stat().st_size > 65536:
            raise ValueError('observation_settings_file_too_large')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('observation_duplicate_key')
                result[key] = value
            return result
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique)

    def _accepted(self):
        if not self.path.exists():
            return ObservationPolicy()
        doc = self._read(self.path)
        if doc.get('schema') != SCHEMA:
            raise ValueError('observation_settings_schema_unsupported')
        return ObservationPolicy.from_settings(doc['settings'])

    def load(self):
        current = self._accepted()
        if not self.import_path.exists():
            return ObservationImportReceipt(current)
        try:
            return self.import_document(self._read(self.import_path))
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            return ObservationImportReceipt(current, errors=(type(exc).__name__ + ':' + str(exc),))

    def import_document(self, document):
        if not isinstance(document, dict) or document.get('schema') != SCHEMA:
            raise ValueError('observation_settings_schema_unsupported')
        current = self._accepted()
        supplied = document.get('settings')
        if not isinstance(supplied, dict):
            raise ValueError('observation_settings_mapping_required')
        merged = current.as_dict()
        unapplied = []
        for section, values in supplied.items():
            if section not in merged:
                unapplied.append(section)
                continue
            if not isinstance(values, dict):
                raise ValueError('observation_section_mapping_required')
            for key, value in values.items():
                if key not in merged[section]:
                    unapplied.append(section + '.' + key)
                else:
                    merged[section][key] = value
        recording = merged['recording']
        requested = supplied.get('recording', {})
        rearm = (recording['investigation_minutes'] != current.recording.investigation_minutes
                 or recording['investigation_id'] != current.recording.investigation_id)
        if 'expires_at' not in requested and rearm:
            recording['expires_at'] = (self.clock() + recording['investigation_minutes']*60
                                       if recording['investigation_minutes'] else 0.)
        policy = ObservationPolicy.from_settings(merged)
        old, new = current.as_dict(), policy.as_dict()
        changed = tuple(s + '.' + k for s in new for k in new[s] if old[s][k] != new[s][k])
        if changed or not self.path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps({'schema': SCHEMA, 'settings': new}, indent=2) + '\n')
        return ObservationImportReceipt(policy, changed, tuple(sorted(unapplied)))

    def export(self, policy, session):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.export_path, json.dumps(dict(schema=SCHEMA,
            settings=policy.as_dict(), session=session), indent=2, allow_nan=False) + '\n')
