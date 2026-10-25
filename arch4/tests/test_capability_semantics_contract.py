"""Contract tests: BackendActionSemantics must be consistent with
ModemCapabilities boolean flags for every catalog entry.

Task 2 of hardening batch 2026-04-10.

The relationship between semantics and capability flags:
- traffic_visibility describes what the backend can OBSERVE.
  A backend must not leave visibility at 'none' while claiming traffic
  support. Unknown traffic requires both capability flags explicitly False.
- ca_visibility describes CA observation depth. If
  supports_ca_details is True, ca_visibility must be 'detailed'.
- nr_control_mode describes NR control granularity. If
  supports_nr_band_lock is True, nr_control_mode must NOT be
  'rat_mode' (use 'mixed' or more specific). If nr_control_mode
  is 'none', supports_nr_band_lock must be False.
"""
import pytest

from arch4.modem.catalog import (
    BACKEND_ACTION_SEMANTICS,
    BACKEND_CATALOG,
)


def _all_backends_with_both():
    """Yield (backend, semantics, capabilities) for entries that have both."""
    for backend, sem in BACKEND_ACTION_SEMANTICS.items():
        entry = BACKEND_CATALOG.get(backend)
        if entry:
            yield backend, sem, entry.capabilities


# ── NR control mode vs supports_nr_band_lock ────────────────────

class TestNrControlModeConsistency:

    @pytest.mark.parametrize("backend,sem,caps", list(_all_backends_with_both()),
                             ids=[b for b, _, _ in _all_backends_with_both()])
    def test_nr_band_lock_true_requires_not_rat_mode(self, backend, sem, caps):
        """If supports_nr_band_lock is True, nr_control_mode must not be
        'rat_mode' — rat_mode means RAT toggle only, not NR band locking."""
        if caps.get("supports_nr_band_lock") is True:
            assert sem.nr_control_mode != "rat_mode", (
                f"{backend}: supports_nr_band_lock=True but nr_control_mode=rat_mode. "
                f"Use 'mixed' or a more specific mode."
            )

    @pytest.mark.parametrize("backend,sem,caps", list(_all_backends_with_both()),
                             ids=[b for b, _, _ in _all_backends_with_both()])
    def test_nr_control_none_means_no_nr_band_lock(self, backend, sem, caps):
        """If nr_control_mode is 'none', supports_nr_band_lock must be False."""
        if sem.nr_control_mode == "none":
            assert caps.get("supports_nr_band_lock") is not True, (
                f"{backend}: nr_control_mode=none but supports_nr_band_lock=True"
            )


# ── CA visibility vs supports_ca_details ────────────────────────

class TestCaVisibilityConsistency:

    @pytest.mark.parametrize("backend,sem,caps", list(_all_backends_with_both()),
                             ids=[b for b, _, _ in _all_backends_with_both()])
    def test_ca_details_true_requires_detailed_visibility(self, backend, sem, caps):
        """If supports_ca_details is True, ca_visibility must be 'detailed'."""
        if caps.get("supports_ca_details") is True:
            assert sem.ca_visibility == "detailed", (
                f"{backend}: supports_ca_details=True but ca_visibility={sem.ca_visibility}. "
                f"Should be 'detailed'."
            )

    @pytest.mark.parametrize("backend,sem,caps", list(_all_backends_with_both()),
                             ids=[b for b, _, _ in _all_backends_with_both()])
    def test_ca_visibility_none_means_no_ca_details(self, backend, sem, caps):
        """If ca_visibility is 'none', supports_ca_details must be False."""
        if sem.ca_visibility == "none":
            assert caps.get("supports_ca_details") is not True, (
                f"{backend}: ca_visibility=none but supports_ca_details=True"
            )


# ── Traffic visibility vs supports_traffic_* ────────────────────

class TestTrafficVisibilityDocumented:
    """Visibility and capability flags must agree, including unavailable traffic."""

    @pytest.mark.parametrize("backend,sem,caps", list(_all_backends_with_both()),
                             ids=[b for b, _, _ in _all_backends_with_both()])
    def test_traffic_support_requires_visibility(self, backend, sem, caps):
        """Traffic-capable backends must not leave visibility at 'none'."""
        if caps.get("supports_traffic_presence") or caps.get("supports_traffic_volume"):
            assert sem.traffic_visibility != "none", (
                f"{backend}: traffic support is enabled but traffic_visibility=none"
            )

    def test_every_backend_documents_traffic_visibility(self):
        """Unavailable traffic must be explicit rather than optimistically inferred."""
        for backend, sem, caps in _all_backends_with_both():
            assert sem.traffic_visibility in {"none", "presence", "counters", "full"}, (
                f"{backend}: unexpected traffic_visibility={sem.traffic_visibility}"
            )
            if sem.traffic_visibility == "none":
                assert caps.get("supports_traffic_presence") is False, backend
                assert caps.get("supports_traffic_volume") is False, backend


# ── Cross-cutting: every backend with semantics must have catalog ──

class TestSemanticsHasCatalog:

    @pytest.mark.parametrize("backend", list(BACKEND_ACTION_SEMANTICS.keys()))
    def test_semantics_entry_has_catalog_entry(self, backend):
        """Every backend in BACKEND_ACTION_SEMANTICS must have a
        matching BACKEND_CATALOG entry."""
        assert backend in BACKEND_CATALOG, (
            f"{backend} has semantics but no catalog entry"
        )


# ── Specific fixes locked ──────────────────────────────────────

class TestSpecificFixes:
    """Lock the specific fixes made in this batch so they don't regress."""

    def test_chateau_5g_nr_control_mode_is_mixed(self):
        sem = BACKEND_ACTION_SEMANTICS["chateau_5g"]
        assert sem.nr_control_mode == "mixed"

    def test_chateau_5g_supports_nr_band_lock(self):
        caps = BACKEND_CATALOG["chateau_5g"].capabilities
        assert caps["supports_nr_band_lock"] is True

    def test_h155_ca_visibility_is_detailed(self):
        sem = BACKEND_ACTION_SEMANTICS["h155"]
        assert sem.ca_visibility == "detailed"

    def test_h155_supports_ca_details(self):
        caps = BACKEND_CATALOG["h155"].capabilities
        assert caps["supports_ca_details"] is True

    def test_nr5103e_ca_visibility_is_detailed(self):
        sem = BACKEND_ACTION_SEMANTICS["nr5103e"]
        assert sem.ca_visibility == "detailed"

    def test_nr5103e_supports_ca_details(self):
        caps = BACKEND_CATALOG["nr5103e"].capabilities
        assert caps["supports_ca_details"] is True
