"""Contract tests: ModemCapabilities defaults stay conservative for
modem-native control levers while traffic support defaults optimistic.

Task 3 of hardening batch 2026-04-10.

Bare ``ModemCapabilities()`` must claim traffic support plus the local
UL-anchor lever by default.
Unknown or partially wired backends still fail closed for control levers.
"""
import pytest

from arch4.interfaces import ModemCapabilities, get_capabilities
from arch4.modem.catalog import BACKEND_CATALOG


ALL_CAPABILITY_FIELDS = [
    "supports_band_selection",
    "supports_band_lock",
    "supports_cell_lock",
    "supports_traffic_presence",
    "supports_traffic_volume",
    "supports_ue_capability",
    "supports_restart",
    "supports_neighbors",
    "supports_ca_details",
    "supports_band_priority",
    "supports_plmn",
    "supports_pband",
    "supports_nr_band_lock",
    "supports_wifi_control",
]


# ── bare construction keeps traffic optimistic only ─────────────

class TestBareConstructionDefaults:

    @pytest.mark.parametrize("field", ALL_CAPABILITY_FIELDS)
    def test_bare_default_matches_policy(self, field):
        """Traffic defaults True; PBAND stays locally available."""
        caps = ModemCapabilities()
        expected = field in {
            "supports_traffic_presence",
            "supports_traffic_volume",
            "supports_pband",
        }
        assert getattr(caps, field) is expected, (
            f"ModemCapabilities().{field} expected {expected} "
            f"but got {getattr(caps, field)}"
        )

    def test_bare_construction_claims_only_traffic_support(self):
        """Bare construction should expose optimistic traffic and local PBAND."""
        caps = ModemCapabilities()
        true_fields = [f for f in ALL_CAPABILITY_FIELDS if getattr(caps, f)]
        assert true_fields == [
            "supports_traffic_presence",
            "supports_traffic_volume",
            "supports_pband",
        ], (
            f"Bare ModemCapabilities() claims: {true_fields}"
        )


# ── unknown backend keeps the same optimistic traffic baseline ──

class TestUnknownBackendDefaults:

    def test_unknown_backend_only_claims_traffic_support(self):
        """Unknown backends inherit optimistic traffic only."""
        caps = get_capabilities("nonexistent_backend_xyz")
        true_fields = [f for f in ALL_CAPABILITY_FIELDS if getattr(caps, f)]
        assert true_fields == [
            "supports_traffic_presence",
            "supports_traffic_volume",
        ], (
            f"Unknown backend got capabilities: {true_fields}"
        )

    def test_empty_string_backend_only_claims_traffic_support(self):
        caps = get_capabilities("")
        true_fields = [f for f in ALL_CAPABILITY_FIELDS if getattr(caps, f)]
        assert true_fields == [
            "supports_traffic_presence",
            "supports_traffic_volume",
        ], (
            f"Empty backend got capabilities: {true_fields}"
        )


# ── real backends keep their declared truth ─────────────────────

BACKENDS_WITH_BAND_SELECTION = [
    b for b, e in BACKEND_CATALOG.items()
    if e.capabilities.get("supports_band_selection") is True
]

BACKENDS_WITH_NEIGHBORS = [
    b for b, e in BACKEND_CATALOG.items()
    if e.capabilities.get("supports_neighbors") is True
]

BACKENDS_WITH_CA_DETAILS = [
    b for b, e in BACKEND_CATALOG.items()
    if e.capabilities.get("supports_ca_details") is True
]

BACKENDS_WITH_BAND_PRIORITY = [
    b for b, e in BACKEND_CATALOG.items()
    if e.capabilities.get("supports_band_priority") is True
]


class TestRealBackendsKeepTruth:
    """Verify that backends which previously relied on permissive defaults
    still get their capabilities through the catalog path."""

    @pytest.mark.parametrize("backend", BACKENDS_WITH_BAND_SELECTION)
    def test_band_selection_preserved(self, backend):
        caps = get_capabilities(backend)
        assert caps.supports_band_selection is True, (
            f"{backend} lost supports_band_selection"
        )

    @pytest.mark.parametrize("backend", BACKENDS_WITH_NEIGHBORS)
    def test_neighbors_preserved(self, backend):
        caps = get_capabilities(backend)
        assert caps.supports_neighbors is True, (
            f"{backend} lost supports_neighbors"
        )

    @pytest.mark.parametrize("backend", BACKENDS_WITH_CA_DETAILS)
    def test_ca_details_preserved(self, backend):
        caps = get_capabilities(backend)
        assert caps.supports_ca_details is True, (
            f"{backend} lost supports_ca_details"
        )

    @pytest.mark.parametrize("backend", BACKENDS_WITH_BAND_PRIORITY)
    def test_band_priority_preserved(self, backend):
        caps = get_capabilities(backend)
        assert caps.supports_band_priority is True, (
            f"{backend} lost supports_band_priority"
        )


# ── full catalog round-trip ─────────────────────────────────────

class TestCatalogRoundTrip:
    """Every catalog entry must round-trip through get_capabilities
    and match the catalog's declared truth exactly."""

    @pytest.mark.parametrize("backend", list(BACKEND_CATALOG.keys()))
    def test_catalog_capability_round_trip(self, backend):
        entry = BACKEND_CATALOG[backend]
        caps = get_capabilities(backend)
        for field in ALL_CAPABILITY_FIELDS:
            catalog_val = entry.capabilities.get(field, False)
            runtime_val = getattr(caps, field)
            assert runtime_val == catalog_val, (
                f"{backend}.{field}: catalog={catalog_val} but "
                f"get_capabilities returned {runtime_val}"
            )
