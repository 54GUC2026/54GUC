"""Define Modem Live access and device identity contracts.

Only Modem Live writes Access Type and compact device identity. Reviewed rows
start the in-process Matrix. Modem Live adds a complete connected-device tuple
with a stable code. Incomplete tuples remain provisional and use the zero code.
"""

from __future__ import annotations

from arch4.modem_live.access_profile_owner import access_type_display_label

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, TypeVar
import unicodedata

from arch4.modem_live.profile_catalog import modem_profile_record


ACCESS_TYPE_SCHEMA = "AccessType.v1"
IDENTITY_SCHEMA = "ModemIdentityKey.v1"
CATALOG_SCHEMA = "DecodeCatalogSnapshot.v1"
OWNER = "arch4.modem_live.compatibility_identity"
CODE_NAMESPACE = "54guc.modem-identity.v1"
LEGACY_CODE_SCHEMA = "modem_identity_code.v1.blake2s28"
NORMALIZATION_EPOCH = 1
UNKNOWN_ACCESS_TYPE_ID = "0x0000000000000000"
UNKNOWN_IDENTITY_CODE = "0x0000000000000000"
UNKNOWN_STATUS = "unknown"
PROVISIONAL_STATUS = "provisional"
EXACT_STATUS = "exact"
PUBLICATION_ORDER = (
    CATALOG_SCHEMA,
    ACCESS_TYPE_SCHEMA,
    IDENTITY_SCHEMA,
)

_HEX64 = re.compile(r"^0x[0-9A-F]{16}$")
_UNKNOWN_TEXT = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "missing",
    }
)


class IdentityContractError(ValueError):
    """Report an invalid identity contract or reviewed registry."""


class CatalogValidationError(IdentityContractError):
    """Raised when a catalog cannot replace the last good replica."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return " ".join(text.split()).casefold()


def _known_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in _UNKNOWN_TEXT else text


def _hex64(value: Any, *, field_name: str, allow_zero: bool = False) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("0X"):
        text = "0x" + text[2:]
    if not _HEX64.fullmatch(text):
        raise IdentityContractError(f"{field_name} must be fixed 64-bit hexadecimal")
    if not allow_zero and text == UNKNOWN_IDENTITY_CODE:
        raise IdentityContractError(f"{field_name} cannot use the reserved zero code")
    return text


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        mapped = _mapping(value)
        if mapped:
            return mapped
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _known_text(value)
        if text:
            return text
    return ""


@dataclass(frozen=True, order=True)
class NormalizedIdentityTuple:
    manufacturer: str
    model: str
    hardware_sku: str
    software_version: str
    firmware_version: str
    access_type_id: str

    @classmethod
    def from_values(
        cls,
        *,
        manufacturer: Any,
        model: Any,
        hardware_sku: Any,
        software_version: Any,
        firmware_version: Any,
        access_type_id: Any,
    ) -> "NormalizedIdentityTuple":
        return cls(
            manufacturer=_normalize_text(manufacturer),
            model=_normalize_text(model),
            hardware_sku=_normalize_text(hardware_sku),
            software_version=_normalize_text(software_version),
            firmware_version=_normalize_text(firmware_version),
            access_type_id=_hex64(
                access_type_id,
                field_name="access_type_id",
                allow_zero=True,
            ),
        )

    @property
    def complete(self) -> bool:
        return bool(
            self.manufacturer
            and self.model
            and self.hardware_sku
            and self.software_version
            and self.firmware_version
            and self.access_type_id != UNKNOWN_ACCESS_TYPE_ID
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "hardware_sku": self.hardware_sku,
            "software_version": self.software_version,
            "firmware_version": self.firmware_version,
            "access_type_id": self.access_type_id,
        }

    @property
    def combination_guard(self) -> str:
        return _checksum(
            {
                "normalization_epoch": NORMALIZATION_EPOCH,
                "tuple": self.as_dict(),
            }
        )


@dataclass(frozen=True)
class ReviewedAccessTypeEntry:
    access_type_id: str
    classifier_key: str
    display_name: str
    semantic_vector_json: str
    review_reference: str

    @classmethod
    def reviewed(
        cls,
        *,
        access_type_id: str,
        classifier_key: str,
        display_name: str,
        semantic_vector: Mapping[str, Any],
        review_reference: str,
    ) -> "ReviewedAccessTypeEntry":
        if not str(review_reference or "").strip():
            raise IdentityContractError("reviewed Access Type requires review_reference")
        if not _normalize_text(classifier_key):
            raise IdentityContractError("reviewed Access Type requires classifier_key")
        return cls(
            access_type_id=_hex64(access_type_id, field_name="access_type_id"),
            classifier_key=_normalize_text(classifier_key),
            display_name=str(display_name or classifier_key).strip(),
            semantic_vector_json=_canonical_json(dict(semantic_vector or {})),
            review_reference=str(review_reference).strip(),
        )

    @property
    def semantic_vector(self) -> dict[str, Any]:
        return json.loads(self.semantic_vector_json)

    @property
    def semantic_fingerprint(self) -> str:
        return _checksum(self.semantic_vector)

    def as_catalog_dict(self) -> dict[str, Any]:
        return {
            "access_type_id": self.access_type_id,
            "classifier_key": self.classifier_key,
            "display_name": self.display_name,
            "semantic_fingerprint": self.semantic_fingerprint,
            "semantic_vector": self.semantic_vector,
            "review_reference": self.review_reference,
        }


@dataclass(frozen=True)
class ReviewedIdentityEntry:
    identity_code: str
    identity_tuple: NormalizedIdentityTuple
    display_json: str
    review_reference: str

    @classmethod
    def reviewed(
        cls,
        *,
        identity_code: str,
        manufacturer: str,
        model: str,
        hardware_sku: str,
        software_version: str,
        firmware_version: str,
        access_type_id: str,
        display: Mapping[str, Any] | None = None,
        review_reference: str,
    ) -> "ReviewedIdentityEntry":
        if not str(review_reference or "").strip():
            raise IdentityContractError("reviewed identity requires review_reference")
        identity_tuple = NormalizedIdentityTuple.from_values(
            manufacturer=manufacturer,
            model=model,
            hardware_sku=hardware_sku,
            software_version=software_version,
            firmware_version=firmware_version,
            access_type_id=access_type_id,
        )
        if not identity_tuple.complete:
            raise IdentityContractError("reviewed identity tuple must be complete")
        decoded_display = dict(display or {})
        decoded_display.setdefault("manufacturer", manufacturer)
        decoded_display.setdefault("model", model)
        decoded_display.setdefault("hardware_sku", hardware_sku)
        decoded_display.setdefault("software_version", software_version)
        decoded_display.setdefault("firmware_version", firmware_version)
        return cls(
            identity_code=_hex64(identity_code, field_name="identity_code"),
            identity_tuple=identity_tuple,
            display_json=_canonical_json(decoded_display),
            review_reference=str(review_reference).strip(),
        )

    @property
    def display(self) -> dict[str, Any]:
        return json.loads(self.display_json)

    def as_catalog_dict(self, *, access_type_fingerprint: str) -> dict[str, Any]:
        return {
            "identity_code": self.identity_code,
            "normalized_tuple": self.identity_tuple.as_dict(),
            "decoded_display": self.display,
            "combination_guard": self.identity_tuple.combination_guard,
            "access_type_fingerprint": access_type_fingerprint,
            "review_reference": self.review_reference,
        }


@dataclass(frozen=True)
class ReviewedLegacyIdentityMigration:
    """Reviewed one-way mapping from an old hash to a catalog identity.

    The old code is not globally unique. Migration requires all recorded
    evidence. This table cannot allocate identities or look up by old code only.
    """

    legacy_code: str
    target_identity_code: str
    evidence_json: str
    review_reference: str

    @classmethod
    def reviewed(
        cls,
        *,
        legacy_code: str,
        target_identity_code: str,
        evidence: Mapping[str, Any],
        review_reference: str,
    ) -> "ReviewedLegacyIdentityMigration":
        legacy = str(legacy_code or "").strip().upper()
        if legacy.startswith("0X"):
            legacy = "0x" + legacy[2:]
        if not re.fullmatch(r"0x[0-9A-F]{8}", legacy) or legacy == "0x00000000":
            raise IdentityContractError("legacy identity code must be nonzero 32-bit hexadecimal")
        if not str(review_reference or "").strip():
            raise IdentityContractError("legacy identity migration requires review_reference")
        normalized_evidence = {
            str(key): _normalize_text(value)
            for key, value in dict(evidence or {}).items()
            if _known_text(value)
        }
        required = {
            "manufacturer",
            "model",
            "hardware_sku",
            "software_version",
            "firmware_version",
            "profile_id",
            "backend",
            "webui",
        }
        if not required.issubset(normalized_evidence):
            raise IdentityContractError("legacy identity migration requires complete exact evidence")
        return cls(
            legacy_code=legacy,
            target_identity_code=_hex64(
                target_identity_code,
                field_name="target_identity_code",
            ),
            evidence_json=_canonical_json(normalized_evidence),
            review_reference=str(review_reference).strip(),
        )

    @property
    def evidence(self) -> dict[str, str]:
        return json.loads(self.evidence_json)

    @property
    def evidence_guard(self) -> str:
        return _checksum(self.evidence)

    def as_catalog_dict(self) -> dict[str, Any]:
        return {
            "legacy_schema": LEGACY_CODE_SCHEMA,
            "legacy_code": self.legacy_code,
            "target_identity_code": self.target_identity_code,
            "evidence": self.evidence,
            "evidence_guard": self.evidence_guard,
            "review_reference": self.review_reference,
        }


@dataclass(frozen=True)
class DecodeCatalogSnapshot:
    payload_json: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DecodeCatalogSnapshot":
        validated = _validate_catalog_payload(payload)
        return cls(payload_json=_canonical_json(validated))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @property
    def catalog_version(self) -> int:
        return int(self.as_dict()["catalog_version"])

    @property
    def catalog_checksum(self) -> str:
        return str(self.as_dict()["catalog_checksum"])

    @property
    def previous_catalog_checksum(self) -> str:
        return str(self.as_dict().get("previous_catalog_checksum") or "")


def _validate_catalog_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    if data.get("schema") != CATALOG_SCHEMA or data.get("owner") != OWNER:
        raise CatalogValidationError("invalid decode-catalog schema or owner")
    if data.get("code_namespace") != CODE_NAMESPACE:
        raise CatalogValidationError("decode-catalog namespace mismatch")
    if int(data.get("normalization_epoch", 0) or 0) != NORMALIZATION_EPOCH:
        raise CatalogValidationError("decode-catalog normalization epoch mismatch")
    version = int(data.get("catalog_version", 0) or 0)
    if version <= 0:
        raise CatalogValidationError("decode-catalog version must be positive")
    previous_checksum = str(data.get("previous_catalog_checksum") or "")
    if version == 1 and previous_checksum:
        raise CatalogValidationError("initial decode-catalog cannot declare predecessor")
    if version > 1 and not previous_checksum:
        raise CatalogValidationError("appended decode-catalog requires predecessor checksum")
    if data.get("code_format") != "uint64_hex_0x16_upper":
        raise CatalogValidationError("decode-catalog code format mismatch")
    if int(data.get("access_type_catalog_version", 0) or 0) != version:
        raise CatalogValidationError("Access Type catalog version mismatch")
    if not str(data.get("generation_review_reference") or "").strip():
        raise CatalogValidationError("decode-catalog generation review reference missing")

    access_types = data.get("access_types")
    identities = data.get("entries")
    legacy_migrations = data.get("legacy_migrations")
    if (
        not isinstance(access_types, list)
        or not isinstance(identities, list)
        or not isinstance(legacy_migrations, list)
    ):
        raise CatalogValidationError("decode-catalog entries must be lists")
    access_ids: set[str] = set()
    classifier_keys: set[str] = set()
    for row in access_types:
        if not isinstance(row, Mapping):
            raise CatalogValidationError("invalid Access Type catalog row")
        access_id = _hex64(row.get("access_type_id"), field_name="access_type_id")
        classifier_key = _normalize_text(row.get("classifier_key"))
        semantic_vector = _mapping(row.get("semantic_vector"))
        if access_id in access_ids or not classifier_key or classifier_key in classifier_keys:
            raise CatalogValidationError("duplicate Access Type ID or classifier key")
        if str(row.get("semantic_fingerprint") or "") != _checksum(semantic_vector):
            raise CatalogValidationError("Access Type semantic fingerprint mismatch")
        if not str(row.get("review_reference") or "").strip():
            raise CatalogValidationError("Access Type review reference missing")
        access_ids.add(access_id)
        classifier_keys.add(classifier_key)

    if str(data.get("access_type_catalog_checksum") or "") != _checksum(access_types):
        raise CatalogValidationError("Access Type catalog checksum mismatch")

    codes: set[str] = set()
    tuples: set[str] = set()
    access_fingerprints = {
        str(row["access_type_id"]): str(row["semantic_fingerprint"])
        for row in access_types
    }
    for row in identities:
        if not isinstance(row, Mapping):
            raise CatalogValidationError("invalid identity catalog row")
        code = _hex64(row.get("identity_code"), field_name="identity_code")
        tuple_map = _mapping(row.get("normalized_tuple"))
        identity_tuple = NormalizedIdentityTuple.from_values(
            manufacturer=tuple_map.get("manufacturer"),
            model=tuple_map.get("model"),
            hardware_sku=tuple_map.get("hardware_sku"),
            software_version=tuple_map.get("software_version"),
            firmware_version=tuple_map.get("firmware_version"),
            access_type_id=tuple_map.get("access_type_id"),
        )
        tuple_key = _canonical_json(identity_tuple.as_dict())
        if not identity_tuple.complete or code in codes or tuple_key in tuples:
            raise CatalogValidationError("duplicate or incomplete identity catalog row")
        if identity_tuple.access_type_id not in access_ids:
            raise CatalogValidationError("identity references unknown Access Type")
        if str(row.get("combination_guard") or "") != identity_tuple.combination_guard:
            raise CatalogValidationError("identity combination guard mismatch")
        if str(row.get("access_type_fingerprint") or "") != access_fingerprints[identity_tuple.access_type_id]:
            raise CatalogValidationError("identity Access Type fingerprint mismatch")
        if not isinstance(row.get("decoded_display"), Mapping):
            raise CatalogValidationError("identity decoded display is missing")
        if not str(row.get("review_reference") or "").strip():
            raise CatalogValidationError("identity review reference missing")
        codes.add(code)
        tuples.add(tuple_key)

    legacy_keys: set[tuple[str, str]] = set()
    for row in legacy_migrations:
        if not isinstance(row, Mapping):
            raise CatalogValidationError("invalid legacy migration catalog row")
        if str(row.get("legacy_schema") or "") != LEGACY_CODE_SCHEMA:
            raise CatalogValidationError("legacy migration schema mismatch")
        legacy_code = str(row.get("legacy_code") or "").strip().upper()
        if legacy_code.startswith("0X"):
            legacy_code = "0x" + legacy_code[2:]
        if not re.fullmatch(r"0x[0-9A-F]{8}", legacy_code) or legacy_code == "0x00000000":
            raise CatalogValidationError("invalid legacy migration code")
        target_code = _hex64(
            row.get("target_identity_code"),
            field_name="target_identity_code",
        )
        if target_code not in codes:
            raise CatalogValidationError("legacy migration target is absent from catalog")
        evidence = {
            str(key): _normalize_text(value)
            for key, value in _mapping(row.get("evidence")).items()
            if _known_text(value)
        }
        required_evidence = {
            "manufacturer",
            "model",
            "hardware_sku",
            "software_version",
            "firmware_version",
            "profile_id",
            "backend",
            "webui",
        }
        if not required_evidence.issubset(evidence):
            raise CatalogValidationError("legacy migration exact evidence is incomplete")
        if str(row.get("evidence_guard") or "") != _checksum(evidence):
            raise CatalogValidationError("legacy migration evidence guard mismatch")
        migration_key = (legacy_code, _canonical_json(evidence))
        if migration_key in legacy_keys:
            raise CatalogValidationError("duplicate legacy migration evidence alias")
        if not str(row.get("review_reference") or "").strip():
            raise CatalogValidationError("legacy migration review reference missing")
        legacy_keys.add(migration_key)

    supplied_checksum = str(data.get("catalog_checksum") or "")
    checksum_payload = dict(data)
    checksum_payload.pop("catalog_checksum", None)
    if supplied_checksum != _checksum(checksum_payload):
        raise CatalogValidationError("decode-catalog checksum mismatch")
    return data


def _access_semantics(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k:v for k,v in row.items() if k not in {'display_name','review_reference'}}


def _assert_reviewed_description_update(previous: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    if (_access_semantics(previous)!=_access_semantics(candidate)
            or previous.get('review_reference')==candidate.get('review_reference')):
        raise CatalogValidationError('decode-catalog descriptive update review or immutable semantics invalid')


def _assert_append_only_catalog(
    previous: DecodeCatalogSnapshot,
    candidate: DecodeCatalogSnapshot,
) -> None:
    previous_payload = previous.as_dict()
    candidate_payload = candidate.as_dict()
    for field in ("access_types", "entries", "legacy_migrations"):
        # Codes, classifiers and semantics are immutable. Reviewed descriptive
        # corrections belong to the next checksummed generation, not a GUI alias.
        prior_rows = {
            _canonical_json(_access_semantics(row) if field=='access_types' else row)
            for row in previous_payload.get(field, [])
            if isinstance(row, Mapping)
        }
        next_rows = {
            _canonical_json(_access_semantics(row) if field=='access_types' else row)
            for row in candidate_payload.get(field, [])
            if isinstance(row, Mapping)
        }
        if not prior_rows.issubset(next_rows):
            raise CatalogValidationError(
                f"decode-catalog append-only violation in {field}"
            )
        if field=='access_types':
            prior_by_id={row['access_type_id']:row for row in previous_payload[field]}
            for row in candidate_payload[field]:
                old=prior_by_id.get(row['access_type_id'])
                if old is not None and row!=old:
                    _assert_reviewed_description_update(old,row)


class ReviewedIdentityRegistry:
    """Immutable append-only catalog generation used by Modem Live Matrix."""

    def __init__(
        self,
        *,
        access_types: Iterable[ReviewedAccessTypeEntry] = (),
        identities: Iterable[ReviewedIdentityEntry] = (),
        legacy_migrations: Iterable[ReviewedLegacyIdentityMigration] = (),
        catalog_version: int = 1,
        previous_catalog_checksum: str = "",
        created_at: float = 0.0,
        generation_review_reference: str = "registry_initial_generation",
    ) -> None:
        access_rows = tuple(sorted(tuple(access_types), key=lambda row: row.access_type_id))
        identity_rows = tuple(sorted(tuple(identities), key=lambda row: row.identity_code))
        legacy_rows = tuple(
            sorted(
                tuple(legacy_migrations),
                key=lambda row: (row.legacy_code, row.evidence_guard),
            )
        )
        access_by_id: dict[str, ReviewedAccessTypeEntry] = {}
        access_by_key: dict[str, ReviewedAccessTypeEntry] = {}
        for row in access_rows:
            if row.access_type_id in access_by_id or row.classifier_key in access_by_key:
                raise IdentityContractError("duplicate reviewed Access Type")
            access_by_id[row.access_type_id] = row
            access_by_key[row.classifier_key] = row

        identity_by_code: dict[str, ReviewedIdentityEntry] = {}
        identity_by_tuple: dict[NormalizedIdentityTuple, ReviewedIdentityEntry] = {}
        catalog_entries: list[dict[str, Any]] = []
        for row in identity_rows:
            if row.identity_tuple.access_type_id not in access_by_id:
                raise IdentityContractError("reviewed identity references unknown Access Type")
            if row.identity_code in identity_by_code or row.identity_tuple in identity_by_tuple:
                raise IdentityContractError("reviewed identity code/tuple is not bijective")
            identity_by_code[row.identity_code] = row
            identity_by_tuple[row.identity_tuple] = row
            catalog_entries.append(
                row.as_catalog_dict(
                    access_type_fingerprint=access_by_id[row.identity_tuple.access_type_id].semantic_fingerprint
                )
            )

        legacy_keys: set[tuple[str, str]] = set()
        catalog_migrations: list[dict[str, Any]] = []
        for row in legacy_rows:
            if row.target_identity_code not in identity_by_code:
                raise IdentityContractError("legacy migration target is not reviewed")
            key = (row.legacy_code, row.evidence_guard)
            if key in legacy_keys:
                raise IdentityContractError("duplicate reviewed legacy migration")
            legacy_keys.add(key)
            catalog_migrations.append(row.as_catalog_dict())

        access_catalog = [row.as_catalog_dict() for row in access_rows]
        catalog_payload = {
            "schema": CATALOG_SCHEMA,
            "owner": OWNER,
            "code_namespace": CODE_NAMESPACE,
            "catalog_version": int(catalog_version),
            "normalization_epoch": NORMALIZATION_EPOCH,
            "created_at": float(created_at or 0.0),
            "previous_catalog_checksum": str(previous_catalog_checksum or ""),
            "generation_review_reference": str(
                generation_review_reference or ""
            ).strip(),
            "access_type_catalog_version": int(catalog_version),
            "access_type_catalog_checksum": _checksum(access_catalog),
            "code_format": "uint64_hex_0x16_upper",
            "access_types": access_catalog,
            "entries": catalog_entries,
            "legacy_migrations": catalog_migrations,
        }
        catalog_payload["catalog_checksum"] = _checksum(catalog_payload)
        snapshot = DecodeCatalogSnapshot.from_payload(catalog_payload)

        self._access_types = access_rows
        self._identities = identity_rows
        self._legacy_migrations = legacy_rows
        self._access_by_id = MappingProxyType(access_by_id)
        self._access_by_key = MappingProxyType(access_by_key)
        self._identity_by_code = MappingProxyType(identity_by_code)
        self._identity_by_tuple = MappingProxyType(identity_by_tuple)
        self._catalog_snapshot = snapshot

    @classmethod
    def from_snapshot(cls, snapshot: DecodeCatalogSnapshot) -> "ReviewedIdentityRegistry":
        """Resume the owner's exact validated catalog, without recoding rows."""
        if not isinstance(snapshot, DecodeCatalogSnapshot):
            raise IdentityContractError("owner restore requires a typed decode catalog")
        data = DecodeCatalogSnapshot.from_payload(snapshot.as_dict()).as_dict()
        restored = cls(
            access_types=(ReviewedAccessTypeEntry.reviewed(
                access_type_id=row["access_type_id"], classifier_key=row["classifier_key"],
                display_name=row["display_name"], semantic_vector=row["semantic_vector"],
                review_reference=row["review_reference"],
            ) for row in data["access_types"]),
            identities=(ReviewedIdentityEntry.reviewed(
                identity_code=row["identity_code"], **row["normalized_tuple"],
                display=row["decoded_display"], review_reference=row["review_reference"],
            ) for row in data["entries"]),
            legacy_migrations=(ReviewedLegacyIdentityMigration.reviewed(
                legacy_code=row["legacy_code"], target_identity_code=row["target_identity_code"],
                evidence=row["evidence"], review_reference=row["review_reference"],
            ) for row in data["legacy_migrations"]),
            catalog_version=data["catalog_version"], created_at=data["created_at"],
            previous_catalog_checksum=data["previous_catalog_checksum"],
            generation_review_reference=data["generation_review_reference"],
        )
        if restored.catalog_snapshot.as_dict() != data:
            raise IdentityContractError("owner restore changed the recorded catalog")
        return restored

    @property
    def catalog_snapshot(self) -> DecodeCatalogSnapshot:
        return self._catalog_snapshot

    def classify_access_type(self, classifier_key: Any) -> ReviewedAccessTypeEntry | None:
        key = _normalize_text(classifier_key)
        if not key:
            return None
        if str(classifier_key or "").strip().upper().startswith("0X"):
            try:
                return self._access_by_id.get(
                    _hex64(classifier_key, field_name="access_type_id", allow_zero=True)
                )
            except IdentityContractError:
                return None
        return self._access_by_key.get(key)

    def identity_for_tuple(self, identity_tuple: NormalizedIdentityTuple) -> ReviewedIdentityEntry | None:
        return self._identity_by_tuple.get(identity_tuple)

    def identity_for_code(self, identity_code: Any) -> ReviewedIdentityEntry | None:
        try:
            code = _hex64(
                identity_code,
                field_name="identity_code",
                allow_zero=True,
            )
        except IdentityContractError:
            return None
        return self._identity_by_code.get(code)

    def append_reviewed(
        self,
        *,
        access_types: Iterable[ReviewedAccessTypeEntry] = (),
        access_description_updates: Iterable[ReviewedAccessTypeEntry] = (),
        identities: Iterable[ReviewedIdentityEntry] = (),
        legacy_migrations: Iterable[ReviewedLegacyIdentityMigration] = (),
        expected_catalog_checksum: str,
        review_reference: str,
        created_at: float = 0.0,
    ) -> "ReviewedIdentityRegistry":
        """Return a new immutable generation after an approved append."""

        if expected_catalog_checksum != self.catalog_snapshot.catalog_checksum:
            raise IdentityContractError("reviewed append expected checksum mismatch")
        if not str(review_reference or "").strip():
            raise IdentityContractError("reviewed append requires review_reference")
        new_access = tuple(access_types)
        new_identities = tuple(identities)
        new_migrations = tuple(legacy_migrations)
        descriptions=tuple(access_description_updates)
        if len({row.access_type_id for row in descriptions})!=len(descriptions):
            raise IdentityContractError('reviewed description duplicate ID')
        for row in descriptions:
            old=self._access_by_id.get(row.access_type_id)
            if old is None:raise IdentityContractError('reviewed description prior ID missing')
            _assert_reviewed_description_update(old.as_catalog_dict(), row.as_catalog_dict())
        replacements={row.access_type_id:row for row in descriptions}
        if not new_access and not new_identities and not new_migrations and not descriptions:
            raise IdentityContractError("reviewed append must add at least one entry")
        return ReviewedIdentityRegistry(
            access_types=(*(replacements.get(row.access_type_id,row) for row in self._access_types), *new_access),
            identities=(*self._identities, *new_identities),
            legacy_migrations=(*self._legacy_migrations, *new_migrations),
            catalog_version=self.catalog_snapshot.catalog_version + 1,
            previous_catalog_checksum=self.catalog_snapshot.catalog_checksum,
            created_at=created_at,
            generation_review_reference=str(review_reference).strip(),
        )


DEFAULT_REVIEWED_IDENTITY_REGISTRY = ReviewedIdentityRegistry()


def _effective_identity_code(identity_tuple: NormalizedIdentityTuple) -> str:
    """Return a stable Modem Live code for one complete normalized tuple."""

    if not identity_tuple.complete:
        raise IdentityContractError(
            "effective identity code requires a complete normalized tuple"
        )
    digest = sha256(
        _canonical_json(
            {
                "code_namespace": CODE_NAMESPACE,
                "normalization_epoch": NORMALIZATION_EPOCH,
                "normalized_tuple": identity_tuple.as_dict(),
            }
        ).encode("utf-8")
    ).hexdigest()
    code = "0x" + digest[:16].upper()
    if code == UNKNOWN_IDENTITY_CODE:
        raise IdentityContractError("effective identity code resolved to reserved zero")
    return code


class ModemLiveIdentityMatrix:
    """Store connected-device identity rows for the running process."""

    def __init__(
        self, seed_registry: ReviewedIdentityRegistry, *,
        restored_catalog: DecodeCatalogSnapshot | None = None,
    ) -> None:
        if not isinstance(seed_registry, ReviewedIdentityRegistry):
            raise IdentityContractError(
                "Modem Live identity matrix requires a reviewed seed registry"
            )
        self._seed_registry = seed_registry
        self._registry = (ReviewedIdentityRegistry.from_snapshot(restored_catalog)
                          if restored_catalog is not None else seed_registry)

        description_updates=[]
        def additions(current, incoming, keys, *, descriptions=False):
            indexes = [{key(row): row for row in current} for key in keys]
            missing = []
            for row in incoming:
                matches = [index.get(key(row)) for index, key in zip(indexes, keys)]
                differing=[match for match in matches if match is not None and match!=row]
                if differing:
                    if not descriptions:raise IdentityContractError("reviewed seed conflicts with restored catalog")
                    for match in differing:
                        _assert_reviewed_description_update(match.as_catalog_dict(), row.as_catalog_dict())
                    description_updates.append(row)
                if not any(match is not None for match in matches):
                    missing.append(row)
            return tuple(missing)

        self._pending_access = additions(self._registry._access_types,
            seed_registry._access_types, (lambda row: row.access_type_id, lambda row: row.classifier_key),descriptions=True)
        self._pending_descriptions=tuple(description_updates)
        self._pending_identities = additions(self._registry._identities,
            seed_registry._identities, (lambda row: row.identity_code, lambda row: row.identity_tuple))
        self._pending_migrations = additions(self._registry._legacy_migrations,
            seed_registry._legacy_migrations, (lambda row: (row.legacy_code, row.evidence_guard),))
        self._lock = RLock()

    @property
    def catalog_snapshot(self) -> DecodeCatalogSnapshot:
        with self._lock:
            return self._registry.catalog_snapshot

    def classify_access_type(
        self,
        classifier_key: Any,
    ) -> ReviewedAccessTypeEntry | None:
        with self._lock:
            # The current reviewed seed is the access-contract input. Pending
            # additions become public only in the next atomic identity publication.
            return self._seed_registry.classify_access_type(classifier_key)

    def resolve_identity(
        self,
        identity_tuple: NormalizedIdentityTuple,
        *,
        display: Mapping[str, Any],
    ) -> ReviewedIdentityEntry | None:
        """Publish pending reviewed inputs and resolve one observed tuple."""

        with self._lock:
            existing = self._registry.identity_for_tuple(identity_tuple)
            if existing is None:
                existing = self._seed_registry.identity_for_tuple(identity_tuple)
            observed = None
            if identity_tuple.complete and existing is None:
                identity_code = _effective_identity_code(identity_tuple)
                collision = self._registry.identity_for_code(identity_code)
                if collision is not None and collision.identity_tuple != identity_tuple:
                    raise IdentityContractError("Modem Live effective identity code collision")
                observed = ReviewedIdentityEntry.reviewed(
                    identity_code=identity_code,
                    **identity_tuple.as_dict(), display=dict(display or {}),
                    review_reference="modem_live_effective_identity_matrix.v1",
                )
            identities = self._pending_identities + ((observed,) if observed else ())
            if self._pending_access or identities or self._pending_migrations or self._pending_descriptions:
                self._registry = self._registry.append_reviewed(
                    access_types=self._pending_access, identities=identities,
                    access_description_updates=self._pending_descriptions,
                    legacy_migrations=self._pending_migrations,
                    expected_catalog_checksum=self._registry.catalog_snapshot.catalog_checksum,
                    review_reference="modem_live_effective_identity_matrix.v1",
                    created_at=0.0,
                )
                self._pending_access = self._pending_identities = self._pending_migrations = ()
                self._pending_descriptions=()
            return existing or observed


class DecodeCatalogReplica:
    """Validated immutable read replica that keeps the last good catalog."""

    def __init__(self) -> None:
        self._current: DecodeCatalogSnapshot | None = None
        self._last_error = ""
        self._lock = RLock()

    @property
    def current(self) -> DecodeCatalogSnapshot | None:
        with self._lock:
            return self._current

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def install(self, payload: Mapping[str, Any]) -> DecodeCatalogSnapshot:
        return self.validate_publication(payload, lambda candidate: candidate)

    def validate_publication(
        self,
        payload: Mapping[str, Any],
        validator: Callable[[DecodeCatalogSnapshot], "_ValidatedPublication"],
    ) -> "_ValidatedPublication":
        """Validate the catalog and publication before installing both."""

        with self._lock:
            try:
                candidate = DecodeCatalogSnapshot.from_payload(payload)
                current = self._current
                if current is not None:
                    if candidate.catalog_version < current.catalog_version:
                        raise CatalogValidationError("decode-catalog publication is stale")
                    if candidate.catalog_version == current.catalog_version:
                        if candidate.catalog_checksum != current.catalog_checksum:
                            raise CatalogValidationError("same catalog version has different checksum")
                        candidate = current
                    else:
                        if candidate.catalog_version != current.catalog_version + 1:
                            raise CatalogValidationError("decode-catalog version gap")
                        if candidate.previous_catalog_checksum != current.catalog_checksum:
                            raise CatalogValidationError("decode-catalog lineage mismatch")
                        _assert_append_only_catalog(current, candidate)
                result = validator(candidate)
                self._current = candidate
                self._last_error = ""
                return result
            except (IdentityContractError, KeyError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                if isinstance(exc, IdentityContractError):
                    raise
                raise CatalogValidationError(str(exc)) from exc

    def install_candidate(self, payload: Mapping[str, Any]) -> DecodeCatalogSnapshot:
        """Compatibility method for installing only the catalog replica."""

        return self.install(payload)

    def identity_entry(self, identity_code: Any) -> dict[str, Any] | None:
        with self._lock:
            return _identity_entry_from_snapshot(self._current, identity_code)

    def access_type_entry(self, access_type_id: Any) -> dict[str, Any] | None:
        with self._lock:
            return _access_type_entry_from_snapshot(self._current, access_type_id)

    def legacy_migration(
        self,
        legacy_code: Any,
        *,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return a reviewed alias only when all evidence agrees."""

        with self._lock:
            return _legacy_migration_from_snapshot(self._current, legacy_code, evidence=evidence)


_ValidatedPublication = TypeVar("_ValidatedPublication")


def _identity_entry_from_snapshot(
    snapshot: DecodeCatalogSnapshot | None,
    identity_code: Any,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    try:
        code = _hex64(identity_code, field_name="identity_code", allow_zero=True)
    except IdentityContractError:
        return None
    for row in snapshot.as_dict().get("entries", []):
        if str(row.get("identity_code") or "") == code:
            return dict(row)
    return None


def _access_type_entry_from_snapshot(
    snapshot: DecodeCatalogSnapshot | None,
    access_type_id: Any,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    try:
        access_id = _hex64(access_type_id, field_name="access_type_id", allow_zero=True)
    except IdentityContractError:
        return None
    for row in snapshot.as_dict().get("access_types", []):
        if str(row.get("access_type_id") or "") == access_id:
            return dict(row)
    return None


def _legacy_migration_from_snapshot(
    snapshot: DecodeCatalogSnapshot | None,
    legacy_code: Any,
    *,
    evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    code = str(legacy_code or "").strip().upper()
    if code.startswith("0X"):
        code = "0x" + code[2:]
    if not re.fullmatch(r"0x[0-9A-F]{8}", code) or code == "0x00000000":
        return None
    observed = {
        str(key): _normalize_text(value)
        for key, value in dict(evidence or {}).items()
        if _known_text(value)
    }
    for row in snapshot.as_dict().get("legacy_migrations", []):
        expected = _mapping(row.get("evidence"))
        if str(row.get("legacy_code") or "") != code:
            continue
        if expected and all(observed.get(key) == value for key, value in expected.items()):
            return dict(row)
    return None


def _identity_evidence(payload: Mapping[str, Any]) -> dict[str, str]:
    base = _first_mapping(payload.get("_canonical_status"), payload.get("_base_status"), payload)
    published_identity_key = _first_mapping(
        payload.get("modem_identity_key"),
        base.get("modem_identity_key"),
    )
    runtime_identity = _first_mapping(
        payload.get("runtime_identity"),
        base.get("runtime_identity"),
        payload.get("runtime_identity_display"),
        base.get("runtime_identity_display"),
    )
    resolved_card = _first_mapping(
        payload.get("resolved_identity_card"),
        base.get("resolved_identity_card"),
        runtime_identity.get("resolved_identity_card"),
    )
    capability_profile = _first_mapping(
        payload.get("capability_profile"),
        base.get("capability_profile"),
        runtime_identity.get("capability_profile"),
    )
    return {
        "manufacturer": _first_text(
            published_identity_key.get("manufacturer"),
            runtime_identity.get("manufacturer"),
            runtime_identity.get("vendor"),
            resolved_card.get("manufacturer"),
            resolved_card.get("vendor"),
            base.get("manufacturer"),
            base.get("vendor"),
            payload.get("manufacturer"),
            payload.get("vendor"),
        ),
        "model": _first_text(
            published_identity_key.get("model"),
            resolved_card.get("model"),
            runtime_identity.get("model"),
            base.get("model"),
            base.get("router_model"),
            payload.get("model"),
        ),
        "hardware_sku": _first_text(
            published_identity_key.get("hardware_sku"),
            runtime_identity.get("hardware_sku"),
            runtime_identity.get("hardware_version"),
            resolved_card.get("hardware_sku"),
            resolved_card.get("hardware_version"),
            base.get("hardware_sku"),
            base.get("hardware_version"),
            base.get("hw_version"),
            payload.get("hardware_sku"),
            payload.get("hardware_version"),
        ),
        "software_version": _first_text(
            published_identity_key.get("software_version"),
            runtime_identity.get("software_version"),
            base.get("software_version"),
            base.get("software"),
            payload.get("software_version"),
        ),
        "firmware_version": _first_text(
            published_identity_key.get("firmware_version"),
            resolved_card.get("firmware"),
            runtime_identity.get("firmware"),
            base.get("firmware"),
            payload.get("firmware"),
        ),
        "webui": _first_text(
            resolved_card.get("webui"),
            runtime_identity.get("webui"),
            base.get("webui"),
            payload.get("webui"),
        ),
        "profile_id": _first_text(
            published_identity_key.get("profile_id"),
            resolved_card.get("profile_id"),
            runtime_identity.get("resolved_profile_id"),
            runtime_identity.get("profile_id"),
            capability_profile.get("profile_id"),
            base.get("profile_id"),
            payload.get("profile_id"),
        ),
        "backend": _first_text(
            published_identity_key.get("backend"),
            runtime_identity.get("backend"),
            resolved_card.get("backend"),
            base.get("backend"),
            payload.get("backend"),
        ),
        "backend_family": _first_text(
            published_identity_key.get("backend_family"),
            runtime_identity.get("backend_family"),
            resolved_card.get("backend_family"),
            base.get("backend_family"),
            payload.get("backend_family"),
        ),
        "serial": _first_text(runtime_identity.get("serial"), base.get("serial"), payload.get("serial")),
        "imei": _first_text(runtime_identity.get("imei"), base.get("imei"), payload.get("imei")),
        "imsi": _first_text(runtime_identity.get("imsi"), base.get("imsi"), payload.get("imsi")),
        "lan_mac": _first_text(runtime_identity.get("lan_mac"), base.get("lan_mac"), payload.get("lan_mac")),
        "provider_name": _first_text(
            runtime_identity.get("provider_name"), base.get("provider_name"), payload.get("provider_name")
        ),
        "provider_plmn": _first_text(
            runtime_identity.get("provider_plmn"), base.get("provider_plmn"), payload.get("provider_plmn")
        ),
    }


DERIVED_IDENTITY_FIELDS = frozenset({
    "decode_catalog_snapshot", "access_type_contract", "modem_identity_key",
    "identity_publication_order", "schema", "owner",
    "code_namespace", "identity_code", "identity_status", "catalog_version",
    "catalog_checksum", "combination_guard", "access_type_id",
    "access_type_fingerprint", "modem_live_access_profile", "profile_id",
    "backend", "backend_family", "access_type", "runtime_identity",
    "resolved_identity_card", "capability_profile", "static_modem_truth",
})


def build_modem_live_identity_publication(
    status: Mapping[str, Any] | None,
    *,
    access_type_classifier_key: Any,
    matrix: ModemLiveIdentityMatrix,
) -> dict[str, Any]:
    """Build one atomic decode, Access Type, and identity publication."""

    payload = dict(status or {})
    evidence = _identity_evidence(payload)
    selected_profile_record = modem_profile_record(evidence["profile_id"])
    if not isinstance(matrix, ModemLiveIdentityMatrix):
        raise IdentityContractError("Modem Live identity matrix is required")
    reviewed_access = matrix.classify_access_type(access_type_classifier_key)
    access_hint = _known_text(access_type_classifier_key)
    if reviewed_access is None:
        access_contract = {
            "schema": ACCESS_TYPE_SCHEMA,
            "owner": OWNER,
            "access_type_id": UNKNOWN_ACCESS_TYPE_ID,
            "status": PROVISIONAL_STATUS if access_hint else UNKNOWN_STATUS,
            "contract_version": 1,
            "classifier_key": _normalize_text(access_hint),
            "display_name": access_hint,
            "semantic_fingerprint": "",
            "evidence_digest": _checksum(
                {
                    "classifier_key": _normalize_text(access_hint),
                    "manufacturer": _normalize_text(evidence["manufacturer"]),
                    "model": _normalize_text(evidence["model"]),
                    "webui": _normalize_text(evidence["webui"]),
                }
            ),
            "classification_provenance": "modem_live_unreviewed_evidence",
        }
    else:
        access_contract = {
            "schema": ACCESS_TYPE_SCHEMA,
            "owner": OWNER,
            "access_type_id": reviewed_access.access_type_id,
            "status": EXACT_STATUS,
            "contract_version": 1,
            "classifier_key": reviewed_access.classifier_key,
            "display_name": reviewed_access.display_name,
            "semantic_fingerprint": reviewed_access.semantic_fingerprint,
            "evidence_digest": _checksum(
                {
                    "classifier_key": reviewed_access.classifier_key,
                    "manufacturer": _normalize_text(evidence["manufacturer"]),
                    "model": _normalize_text(evidence["model"]),
                    "webui": _normalize_text(evidence["webui"]),
                }
            ),
            "classification_provenance": reviewed_access.review_reference,
        }

    identity_tuple = NormalizedIdentityTuple.from_values(
        manufacturer=evidence["manufacturer"],
        model=evidence["model"],
        hardware_sku=evidence["hardware_sku"],
        software_version=evidence["software_version"],
        firmware_version=evidence["firmware_version"],
        access_type_id=access_contract["access_type_id"],
    )
    reviewed_identity = matrix.resolve_identity(
        identity_tuple,
        display={
            "manufacturer": evidence["manufacturer"],
            "model": evidence["model"],
            "hardware_sku": evidence["hardware_sku"],
            "software_version": evidence["software_version"],
            "firmware_version": evidence["firmware_version"],
            "webui_evidence": evidence["webui"],
            "profile_id": evidence["profile_id"],
            "backend": evidence["backend"],
        },
    )
    has_evidence = any(
        evidence[key]
        for key in (
            "manufacturer",
            "model",
            "hardware_sku",
            "software_version",
            "firmware_version",
        )
    ) or bool(access_hint)
    identity_status = EXACT_STATUS if reviewed_identity is not None else PROVISIONAL_STATUS if has_evidence else UNKNOWN_STATUS
    identity_code = reviewed_identity.identity_code if reviewed_identity is not None else UNKNOWN_IDENTITY_CODE
    combination_guard = identity_tuple.combination_guard if reviewed_identity is not None else ""
    missing_fields = [
        key
        for key, value in (
            ("manufacturer", identity_tuple.manufacturer),
            ("model", identity_tuple.model),
            ("hardware_sku", identity_tuple.hardware_sku),
            ("software_version", identity_tuple.software_version),
            ("firmware_version", identity_tuple.firmware_version),
        )
        if not value
    ]
    if access_contract["status"] != EXACT_STATUS:
        missing_fields.append("access_type")
    catalog = matrix.catalog_snapshot.as_dict()
    identity_key = {
        "schema": IDENTITY_SCHEMA,
        "owner": OWNER,
        "code_namespace": CODE_NAMESPACE,
        "identity_code": identity_code,
        "status": identity_status,
        "identity_status": identity_status,
        "normalized_tuple": identity_tuple.as_dict(),
        "manufacturer": evidence["manufacturer"],
        "model": evidence["model"],
        "hardware_sku": evidence["hardware_sku"],
        "hardware_version": evidence["hardware_sku"],
        "software_version": evidence["software_version"],
        "firmware_version": evidence["firmware_version"],
        "firmware": evidence["firmware_version"],
        "access_type_id": access_contract["access_type_id"],
        "access_type_contract_version": access_contract["contract_version"],
        "access_type_fingerprint": access_contract["semantic_fingerprint"],
        "normalization_epoch": NORMALIZATION_EPOCH,
        "catalog_version": catalog["catalog_version"],
        "catalog_checksum": catalog["catalog_checksum"],
        "profile_id": evidence["profile_id"],
        "backend": evidence["backend"],
        "backend_family": evidence["backend_family"],
        "combination_guard": combination_guard,
        "observation_guard": _checksum(
            {
                "tuple": identity_tuple.as_dict(),
                "access_classifier_key": access_contract["classifier_key"],
            }
        ),
        "evidence_completeness": {
            "complete": not missing_fields,
            "missing_fields": missing_fields,
        },
        "classification_provenance": (
            reviewed_identity.review_reference
            if reviewed_identity is not None
            else "modem_live_unregistered_combination"
        ),
    }
    return {
        # This mutable profile record stays inside Modem Live. LiveRegisterStore
        # does not publish it to Matrix.
        "selected_profile_record": selected_profile_record,
        "decode_catalog_snapshot": catalog,
        "access_type_contract": access_contract,
        "modem_identity_key": identity_key,
        "identity_publication_order": list(PUBLICATION_ORDER),
        "schema": IDENTITY_SCHEMA,
        "owner": OWNER,
        "code_namespace": CODE_NAMESPACE,
        "identity_code": identity_code,
        "identity_status": identity_status,
        "catalog_version": catalog["catalog_version"],
        "catalog_checksum": catalog["catalog_checksum"],
        "combination_guard": combination_guard,
        "access_type": access_type_display_label(access_contract, semantics=(
            reviewed_access.semantic_vector if reviewed_access is not None else None
        )),
        "access_type_id": access_contract["access_type_id"],
        "access_type_fingerprint": access_contract["semantic_fingerprint"],
        "manufacturer": evidence["manufacturer"],
        "vendor": evidence["manufacturer"],
        "model": evidence["model"],
        "hardware_sku": evidence["hardware_sku"],
        "hardware_version": evidence["hardware_sku"],
        "software_version": evidence["software_version"],
        "firmware_version": evidence["firmware_version"],
        "firmware": evidence["firmware_version"],
        "webui": evidence["webui"],
        "profile_id": evidence["profile_id"],
        "backend": evidence["backend"],
        "backend_family": evidence["backend_family"],
        "serial": evidence["serial"],
        "imei": evidence["imei"],
        "imsi": evidence["imsi"],
        "lan_mac": evidence["lan_mac"],
        "provider_name": evidence["provider_name"],
        "provider_plmn": evidence["provider_plmn"],
    }


def validated_identity_publication_copy(
    payload: Mapping[str, Any],
    *,
    replica: DecodeCatalogReplica,
    before_install: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate all identity parts; the store may persist before atomic install."""

    data = dict(payload or {})
    if tuple(data.get("identity_publication_order") or ()) != PUBLICATION_ORDER:
        raise IdentityContractError("identity publication ordering contract mismatch")
    catalog = _mapping(data.get("decode_catalog_snapshot"))
    access = _mapping(data.get("access_type_contract"))
    identity = _mapping(data.get("modem_identity_key"))
    if not catalog or not access or not identity:
        raise IdentityContractError(
            "identity publication requires decode catalog, Access Type and identity"
        )

    def _validate_dependent_contracts(
        candidate: DecodeCatalogSnapshot,
    ) -> dict[str, Any]:
        access_profile = _mapping(data.get("modem_live_access_profile"))
        if access_profile:
            profile_classifier_key = _normalize_text(access_profile.get("profile_id"))
            published_classifier_key = _normalize_text(access.get("classifier_key"))
            if profile_classifier_key != published_classifier_key:
                raise IdentityContractError(
                    "Modem Live access-profile classifier does not match Access Type publication"
                )

        if access.get("schema") != ACCESS_TYPE_SCHEMA or access.get("owner") != OWNER:
            raise IdentityContractError("invalid Access Type publication owner")
        access_status = str(access.get("status") or "")
        access_id = _hex64(
            access.get("access_type_id"),
            field_name="access_type_id",
            allow_zero=True,
        )
        if access_status == EXACT_STATUS:
            access_row = _access_type_entry_from_snapshot(candidate, access_id)
            if access_row is None:
                raise IdentityContractError("exact Access Type is absent from candidate catalog")
            if str(access.get("semantic_fingerprint") or "") != str(
                access_row.get("semantic_fingerprint") or ""
            ):
                raise IdentityContractError("Access Type fingerprint does not match catalog")
        elif access_status in {PROVISIONAL_STATUS, UNKNOWN_STATUS}:
            if access_id != UNKNOWN_ACCESS_TYPE_ID:
                raise IdentityContractError("non-exact Access Type must use reserved zero ID")
        else:
            raise IdentityContractError("invalid Access Type status")

        if identity.get("schema") != IDENTITY_SCHEMA or identity.get("owner") != OWNER:
            raise IdentityContractError("invalid modem identity publication owner")
        if identity.get("code_namespace") != CODE_NAMESPACE:
            raise IdentityContractError("modem identity namespace mismatch")
        if int(identity.get("catalog_version", 0) or 0) != candidate.catalog_version:
            raise IdentityContractError("modem identity catalog version mismatch")
        if str(identity.get("catalog_checksum") or "") != candidate.catalog_checksum:
            raise IdentityContractError("modem identity catalog checksum mismatch")
        if str(identity.get("access_type_id") or "") != access_id:
            raise IdentityContractError("modem identity Access Type mismatch")

        identity_status = str(identity.get("identity_status") or identity.get("status") or "")
        identity_code = _hex64(
            identity.get("identity_code"),
            field_name="identity_code",
            allow_zero=True,
        )
        if identity_status == EXACT_STATUS:
            row = _identity_entry_from_snapshot(candidate, identity_code)
            if row is None:
                raise IdentityContractError("exact modem identity is absent from candidate catalog")
            if _mapping(identity.get("normalized_tuple")) != _mapping(row.get("normalized_tuple")):
                raise IdentityContractError("modem identity tuple does not match catalog")
            if str(identity.get("combination_guard") or "") != str(
                row.get("combination_guard") or ""
            ):
                raise IdentityContractError("modem identity guard does not match catalog")
            if str(identity.get("access_type_fingerprint") or "") != str(
                row.get("access_type_fingerprint") or ""
            ):
                raise IdentityContractError("modem identity Access Type fingerprint mismatch")
        elif identity_status in {PROVISIONAL_STATUS, UNKNOWN_STATUS}:
            if identity_code != UNKNOWN_IDENTITY_CODE:
                raise IdentityContractError("non-exact modem identity must use reserved zero code")
        else:
            raise IdentityContractError("invalid modem identity status")

        out = dict(data)
        out.update(identity)
        out["schema"] = IDENTITY_SCHEMA
        out["owner"] = OWNER
        out["identity_status"] = identity_status
        out["identity_code"] = identity_code
        out["access_type"] = access_type_display_label(access, semantics=(
            _mapping(access_row.get("semantic_vector")) if access_status == EXACT_STATUS else None
        ))
        out["access_type_id"] = access_id
        out["access_type_contract"] = dict(access)
        out["modem_identity_key"] = dict(identity)
        out["decode_catalog_snapshot"] = candidate.as_dict()
        if access_profile:
            out["modem_live_access_profile"] = dict(access_profile)
        if before_install is not None:
            before_install(MappingProxyType(out))
        return out

    return replica.validate_publication(catalog, _validate_dependent_contracts)


__all__ = [
    "ACCESS_TYPE_SCHEMA",
    "CATALOG_SCHEMA",
    "CODE_NAMESPACE",
    "DEFAULT_REVIEWED_IDENTITY_REGISTRY",
    "DecodeCatalogReplica",
    "DecodeCatalogSnapshot",
    "EXACT_STATUS",
    "IDENTITY_SCHEMA",
    "IdentityContractError",
    "NORMALIZATION_EPOCH",
    "OWNER",
    "PROVISIONAL_STATUS",
    "PUBLICATION_ORDER",
    "ReviewedAccessTypeEntry",
    "ReviewedIdentityEntry",
    "ReviewedIdentityRegistry",
    "ReviewedLegacyIdentityMigration",
    "LEGACY_CODE_SCHEMA",
    "ModemLiveIdentityMatrix",
    "UNKNOWN_ACCESS_TYPE_ID",
    "UNKNOWN_IDENTITY_CODE",
    "UNKNOWN_STATUS",
    "build_modem_live_identity_publication",
    "validated_identity_publication_copy",
]
