from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def impact_test_epoch(monkeypatch):
    """Use the synthetic clock's era in pre-existing lifecycle tests only.

    Production cutover/restart/import tests use the real September 16 epoch.
    """
    from arch4.new_horizon import intelligence_impact

    monkeypatch.setattr(intelligence_impact, "INTELLIGENCE_IMPACT_STARTED_AT", 0.0)


@pytest.fixture(autouse=True)
def _close_background_workers_after_test():
    yield
    from arch4.modem_live.acquisition import shutdown_all_live_input_modules_for_tests

    shutdown_all_live_input_modules_for_tests(join_timeout_s=0.2)
