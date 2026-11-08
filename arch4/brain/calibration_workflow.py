"""
Shared calibration phase tokens.

The workflow assessment and runtime export shaping now live in
``calibration_authority.py``. This module remains only as the low-coupling
home for phase identifiers that are also consumed by review/scoring code.
"""

from __future__ import annotations


PHASE_BOOTSTRAP = "bootstrap_initial"
PHASE_COMPARATIVE = "comparative_recheck"
PHASE_ANCHOR_SURVEY = "single_band_anchor_survey"
PHASE_BACKGROUND = "background_self_calibration"

