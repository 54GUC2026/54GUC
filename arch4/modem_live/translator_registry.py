"""Translate vendor data to normalized Modem Live values."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from arch4.lte_mcs import canonical_qam_label

from .canonical_aliases import (
    CANONICAL_ALIAS_BY_LINE_FIELD,
    CANONICAL_LIVE_FIELD_ALIASES,
    MAX_UNKNOWN_ALIAS_EVIDENCE,
    ModemLiveTranslationResult,
    translate_matrix_payloads,
)
from .raw_observation import RawModemObservation
from .profile_matrix_contract import profile_matrix_dispositions
from .truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG


CANONICAL_MODEM_TRANSLATOR_OWNER = "54guc.modem_live.translator_registry"

# Backend status or readback owns these values, not the generic fast
# ``ModemInput``. Source priority is defined only here.
STATUS_AUTHORITY_FIELDS = frozenset(
    set(MODEM_LIVE_TRUTH_FIELD_CATALOG["slow"])
    | {
        field for field in MODEM_LIVE_TRUTH_FIELD_CATALOG["traffic"]
        if field.startswith("traffic_")
    }
    | {
        "backend",
        "radio_state_text",
        "network_type",
        "network_type_ex",
        "ca_active",
        "ca_state_text",
        "carrier_count",
        "carrier_details",
        "carrier_rows",
        "configured_ca_pool_display",
        "selected_band_set",
        "selected_band_set_signature",
        "configured_band_pool",
        "configured_band_pool_signature",
        "configured_band_pool_source",
        "configured_band_pool_status",
        "configured_band_pool_age_s",
        "configured_band_pool_last_sample_ts",
        "tac",
        "rrc",
        "trans_mode",
        "antenna",
        "wan_ip",
        "neighbors",
        "neighbor_count",
        "wifi",
        "wifi_enabled",
        "wifi_24ghz_enabled",
        "wifi_5ghz_enabled",
        "wifi_6ghz_enabled",
        "backend_diagnostic",
        "connectivity_receipt",
        "device_recovery_state",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalObservationProjection:
    """Normalized payloads and bounded evidence for one raw observation."""

    lines: Mapping[str, Mapping[str, Any]]
    cell_dispositions: Mapping[str, str]
    unknown_token_receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ObservedSourceIndex:
    """Normalized lookup table for one raw observation.

    Modem Live normalizes source keys once. Field mapping then uses alias
    lookups instead of scanning and normalizing the full source for every cell.
    """

    source: Any
    entries: tuple[tuple[str, str, Any], ...]


class CanonicalModemTranslatorRegistry:
    """Define source priority and normalize vendor aliases.

    Select the exact acquiring receipt first, then apply source priority inside
    that receipt. A different read cannot supply defaults for its values.
    """

    owner = CANONICAL_MODEM_TRANSLATOR_OWNER

    @staticmethod
    def normalize_bands(value: Any) -> list[int]:
        from arch4.interfaces import normalize_band_list

        return normalize_band_list(value)

    @staticmethod
    def band_signature(value: Any) -> str:
        from arch4.interfaces import band_signature

        return band_signature(value)

    @staticmethod
    def observed_fields(
        observation: RawModemObservation,
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        del observation, fields
        raise RuntimeError(
            "parallel_modem_field_projection_forbidden:"
            "use_one_translate_invocation"
        )

    @staticmethod
    def exact_status_fields(
        observation: RawModemObservation,
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        if not isinstance(observation, RawModemObservation):
            raise TypeError("raw_modem_observation_required")
        return {
            field: copy.deepcopy(observation.status[field])
            for field in fields
            if field in observation.status
        }

    def translate(
        self,
        *,
        observation: RawModemObservation,
        payloads: Mapping[str, Mapping[str, Any]],
        line_names: frozenset[str],
    ) -> CanonicalObservationProjection:
        """Translate values from exactly the receipts that own their timestamps."""
        if not isinstance(observation, RawModemObservation):
            raise TypeError("raw_modem_observation_required")
        unknown = line_names.difference(MODEM_LIVE_TRUTH_FIELD_CATALOG)
        if unknown:
            raise ValueError("modem_live_translation_line_unknown:" + ",".join(sorted(unknown)))
        if not line_names:
            raise ValueError("modem_live_translation_line_required")
        extra = set(payloads).difference(line_names)
        if extra:
            raise ValueError("modem_live_translation_payload_line_not_requested:" + ",".join(sorted(extra)))
        lines = {line: {} for line in line_names}
        evidence: dict[str, Any] = {}
        for receipt in observation.manifest.receipts:
            owned = frozenset(
                cell for cell in receipt.acquired_cells
                if cell.partition(".")[0] in line_names
            )
            if not owned:
                continue
            active = frozenset(cell.partition(".")[0] for cell in owned)
            result = self._translate_sources(
                observation=observation,
                modem=receipt.modem,
                status=receipt.status,
                payloads={
                    line: {
                        field: value
                        for field, value in payloads.get(line, {}).items()
                        if f"{line}.{field}" in owned
                    }
                    for line in active
                },
                line_names=active,
            )
            for cell in owned:
                line, _, field = cell.partition(".")
                if field in result.lines.get(line, {}):
                    lines[line][field] = result.lines[line][field]
            for key, value in result.unknown_token_receipt.items():
                if isinstance(value, list):
                    evidence[key] = (list(evidence.get(key, [])) + value)[
                        :MAX_UNKNOWN_ALIAS_EVIDENCE
                    ]
                else:
                    evidence[key] = value
        profile = observation.status.get("selected_profile_record")
        if "identity" in line_names and "identity.imsi" in observation.acquired_cells:
            from .sim_identity import sim_network_identity
            # The derived cell shares this exact IMSI receipt, including an
            # explicit empty/unknown read. Never borrow a previous SIM here.
            receipt = observation.acquisition_receipt_for_cell("identity.imsi")
            lines["identity"]["sim_network"] = sim_network_identity(str(receipt.status.get("imsi") or ""))
        dispositions = profile_matrix_dispositions(
            profile if isinstance(profile, Mapping) else None,
            nr_mode=lines.get("radio", {}).get("nr_mode"),
        )
        if "radio" in line_names and isinstance(profile, Mapping) and profile.get("field_trust"):
            trust = profile["field_trust"]
            lines["radio"]["tx_power_support"] = {
                component: trust.get("tx_" + component, "untested")
                for component in ("ppusch", "ppucch", "psrs", "pprach")
            }
        return CanonicalObservationProjection(
            lines=MappingProxyType({
                line: MappingProxyType(values) for line, values in lines.items()
            }),
            cell_dispositions=MappingProxyType({
                path: disposition
                for path, disposition in dispositions.items()
                if path.partition(".")[0] in line_names
            }),
            unknown_token_receipt=MappingProxyType(evidence),
        )

    def _translate_sources(
        self,
        *,
        observation: RawModemObservation,
        modem: Any,
        status: Mapping[str, Any],
        payloads: Mapping[str, Mapping[str, Any]],
        line_names: frozenset[str],
    ) -> CanonicalObservationProjection:
        active_lines = line_names
        prepared_payloads = {
            str(line): dict(payload)
            for line, payload in payloads.items()
            if str(line) in active_lines
        }
        generic_conflicts: list[dict[str, Any]] = []
        source_indexes = {
            "status": _build_observed_source_index(status),
            "modem": _build_observed_source_index(modem),
        }
        for line in active_lines:
            fields = MODEM_LIVE_TRUTH_FIELD_CATALOG[line]
            line_payload = prepared_payloads.setdefault(line, {})
            if line in {"identity", "capability"}:
                continue
            for field in fields:
                if field in line_payload:
                    continue
                if (line, field) in CANONICAL_ALIAS_BY_LINE_FIELD:
                    continue
                sources = (
                    (
                        ("status", source_indexes["status"]),
                        ("modem", source_indexes["modem"]),
                    )
                    if field in STATUS_AUTHORITY_FIELDS
                    else (
                        ("modem", source_indexes["modem"]),
                        ("status", source_indexes["status"]),
                    )
                )
                for source_name, source_index in sources:
                    found, value, conflict = _observed_index_value(
                        source_index,
                        field,
                    )
                    if conflict is not None:
                        generic_conflicts.append(
                            {
                                "canonical_path": f"{line}.{field}",
                                "source": source_name,
                                **conflict,
                                "disposition": "quarantined",
                            }
                        )
                        break
                    if found:
                        line_payload[field] = copy.deepcopy(value)
                        break
        from arch4.modem.live_translation import (
            build_modem_live_radio_surface,
            build_modem_live_traffic_surface,
            canonical_neighbor_rows,
            scheduler_field_trust,
        )

        if "slow" in active_lines:
            slow = prepared_payloads.setdefault("slow", {})
            if "neighbors" in slow:
                slow["neighbors"] = canonical_neighbor_rows(
                    slow["neighbors"]
                )
                slow["neighbor_count"] = len(slow["neighbors"])

        surface: dict[str, Any] = {}
        serving_band_available: bool | None = None
        if "radio" in active_lines:
            surface = dict(
                build_modem_live_radio_surface(
                    modem=modem,
                    raw_base_status=status,
                )
            )
            serving_band_available = bool(
                surface.get("serving_band_available", False)
            )
            if not serving_band_available:
                surface.pop("serving_band", None)
                surface.pop("serving_band_signature", None)
            for field in (
                "selected_band_set",
                "active_band_set",
                "band_set",
                "configured_band_pool",
            ):
                if field not in surface:
                    continue
                bands = self.normalize_bands(surface.get(field) or [])
                if field == "configured_band_pool" and not bands:
                    for related in (
                        "configured_band_pool",
                        "configured_band_pool_signature",
                        "configured_band_pool_source",
                        "configured_band_pool_status",
                        "configured_band_pool_age_s",
                        "configured_band_pool_last_sample_ts",
                    ):
                        surface.pop(related, None)
                    continue
                surface[field] = bands
                surface.setdefault(
                    f"{field}_signature",
                    self.band_signature(bands),
                )
        if "radio" in active_lines:
            radio = prepared_payloads.setdefault("radio", {})
            if serving_band_available is False:
                # Generic status discovery runs before radio translation. Remove
                # its zero or default identity when no serving band was observed.
                radio.pop("serving_band", None)
                radio.pop("serving_band_signature", None)
            for field, value in surface.items():
                if field in MODEM_LIVE_TRUTH_FIELD_CATALOG["radio"]:
                    # A scheduled status-only NR receipt has no ModemInput.
                    # Its exact status already supplied these fields above;
                    # defaults from the absent object cannot replace that read.
                    if modem is None and (field.startswith("nr_") or field in {
                            "nr5g_active", "nsa_active", "sa_active", "network_type"}):
                        continue
                    # After translation, normalized radio data wins. Raw status
                    # names must not override Matrix values.
                    radio[field] = value
            if observation.backend == "zte_goform":
                # ZTE typed samples use zero for missing optional identity. Public
                # status preserves key presence, including valid PCI or NCI zero,
                # so it proves whether the cell was observed.
                for field, status_field in {
                    "nr_band": "nr5g_band",
                    "nr_pci": "nr5g_pci",
                    "nr_arfcn": "nr5g_arfcn",
                    "nr_cell_id": "nr5g_cell_id",
                    "nr_rsrp_dbm": "nr5g_rsrp_dbm",
                    "nr_rsrq_db": "nr5g_rsrq_db",
                    "nr_sinr_db": "nr5g_sinr_db",
                }.items():
                    if status_field in status and status[status_field] is None:
                        radio.pop(field, None)
        if "traffic" in active_lines:
            traffic = prepared_payloads.setdefault("traffic", {})
            prepared_payloads["traffic"] = build_modem_live_traffic_surface(
                modem=modem, raw_base_status=traffic,
            )
        if "rf" in active_lines:
            rf = prepared_payloads.setdefault("rf", {})
            field_trust = dict(
                scheduler_field_trust(
                    raw_base_status=status,
                    modem=modem,
                )
            )
            rf["scheduler_field_trust"] = field_trust
            unsupported = sorted(
                field
                for field, trust in field_trust.items()
                if str(trust or "").strip().lower()
                in {"unsupported", "non_useful"}
            )
            rf["scheduler_unsupported_fields"] = unsupported
            for field in unsupported:
                rf.pop(str(field), None)
        result: ModemLiveTranslationResult = translate_matrix_payloads(
            prepared_payloads,
            modem=modem,
            status=status,
            backend=observation.backend,
            profile_id=str(status.get("profile_id") or ""),
            firmware=str(
                status.get("firmware")
                or status.get("firmware_version")
                or ""
            ),
            webui=str(status.get("webui") or ""),
            access_type=str(status.get("access_type") or ""),
            source=observation.source,
        )
        evidence = dict(result.unknown_alias_evidence)
        if generic_conflicts:
            evidence.setdefault(
                "schema",
                "ModemLiveScoutUnknownTokenReceipt.v1",
            )
            evidence.setdefault(
                "owner",
                "54guc.modem_live.canonical_aliases",
            )
            evidence.setdefault("consumer", "54guc.modem_live.scout")
            evidence.setdefault(
                "disposition",
                "investigation_pending",
            )
            existing = list(evidence.get("same_source_conflicts") or ())
            evidence["same_source_conflicts"] = (
                existing + generic_conflicts
            )[:MAX_UNKNOWN_ALIAS_EVIDENCE]
            evidence["bounded"] = True
            evidence["limit"] = MAX_UNKNOWN_ALIAS_EVIDENCE
        projected_lines = {
            str(line): dict(payload)
            for line, payload in result.lines.items()
        }
        for conflict in generic_conflicts:
            line, _, field = conflict["canonical_path"].partition(".")
            if line == "traffic":
                projected_lines.get(line, {}).pop(field, None)
        if observation.backend == "zte_goform" and "radio" in projected_lines:
            radio = projected_lines["radio"]
            for field, status_field in {
                "nr_band": "nr5g_band",
                "nr_pci": "nr5g_pci",
                "nr_arfcn": "nr5g_arfcn",
                "nr_cell_id": "nr5g_cell_id",
                "nr_rsrp_dbm": "nr5g_rsrp_dbm",
                "nr_rsrq_db": "nr5g_rsrq_db",
                "nr_sinr_db": "nr5g_sinr_db",
            }.items():
                if status_field in status and status[status_field] is None:
                    radio.pop(field, None)
        return CanonicalObservationProjection(
            lines=MappingProxyType(
                {
                    str(line): MappingProxyType(dict(payload))
                    for line, payload in projected_lines.items()
                }
            ),
            cell_dispositions=MappingProxyType({}),
            unknown_token_receipt=MappingProxyType(
                evidence
            ),
        )


def _observed(value: Any) -> bool:
    return value is not None and value != ""


def _build_observed_source_index(source: Any) -> _ObservedSourceIndex:
    if isinstance(source, Mapping):
        source_mapping = source
    else:
        try:
            source_mapping = vars(source)
        except TypeError:
            source_mapping = None
    entries: list[tuple[str, str, Any]] = []
    if source_mapping is not None:
        for key, value in source_mapping.items():
            entries.append(
                (str(key), _normalized_source_key(key), value)
            )
    return _ObservedSourceIndex(
        source=source,
        entries=tuple(entries),
    )


def _observed_index_value(
    index: _ObservedSourceIndex,
    field: str,
) -> tuple[bool, Any, dict[str, Any] | None]:
    if not isinstance(index, _ObservedSourceIndex):
        raise TypeError("modem_live_observed_source_index_required")
    aliases = tuple(
        dict.fromkeys(
            (
                str(field),
                *CANONICAL_LIVE_FIELD_ALIASES.get(str(field), ()),
            )
        )
    )
    alias_keys = {
        _normalized_source_key(name)
        for name in aliases
    }
    candidates = [
        (raw_key, value)
        for raw_key, normalized_key, value in index.entries
        if normalized_key in alias_keys and _observed(value)
    ]
    if not index.entries:
        # Explicit boundary for non-enumerable native objects. Both source
        # representations use the single reconciliation block below.
        for name in aliases:
            if not name.isidentifier() or not hasattr(index.source, name):
                continue
            value = getattr(index.source, name)
            if _observed(value):
                candidates.append((name, value))
    if not candidates:
        return False, None, None
    if field in {"qam_dl0", "qam_dl1", "qam_ul"}:
        normalized = []
        for key, value in candidates:
            label = canonical_qam_label(value)
            # Invalid native tokens remain invalid evidence; do not erase a
            # conflicting zero/unknown token in favour of another observation.
            normalized.append((key, label if label in {"QPSK", "16QAM", "64QAM", "256QAM"} else value))
        candidates = normalized
    distinct_values = sorted({repr(value) for _key, value in candidates})
    if len(distinct_values) > 1:
        return False, None, {
            "raw_keys": sorted(key for key, _value in candidates),
            "normalized_values": distinct_values,
        }
    return True, candidates[0][1], None


def _normalized_source_key(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").strip().lower()
        if character.isalnum()
    )


__all__ = (
    "CANONICAL_MODEM_TRANSLATOR_OWNER",
    "CanonicalModemTranslatorRegistry",
    "CanonicalObservationProjection",
    "STATUS_AUTHORITY_FIELDS",
)
