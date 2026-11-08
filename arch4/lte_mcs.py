"""
Shared LTE MCS/Qm helpers.

Policy used across ARCH:
- Nominal modulation evidence comes from MCS 0..27 only.
- MCS 28..31 are treated as retransmission markers, not fresh modulation class.
"""

from typing import Tuple


LTE_RETX_MCS = {28, 29, 30, 31}


def is_lte_retx_mcs(mcs: int) -> bool:
    try:
        return int(mcs) in LTE_RETX_MCS
    except Exception:
        return False


def lte_nominal_qm_from_mcs(mcs: int) -> int:
    """
    Map nominal LTE MCS to modulation order Qm.

    Returns:
      2,4,6,8 for nominal ranges (0..27)
      0 for unknown/non-nominal/retransmission markers (28..31)
    """
    try:
        m = int(mcs)
    except Exception:
        return 0
    if 0 <= m <= 4:
        return 2
    if 5 <= m <= 10:
        return 4
    if 11 <= m <= 19:
        return 6
    if 20 <= m <= 27:
        return 8
    return 0


def resolve_lte_qm_with_retx_hold(mcs: int, last_qm: int = 0) -> Tuple[int, bool]:
    """
    Resolve effective Qm while ignoring MCS 28..31 as fresh evidence.

    Returns:
      (qm, used_retx_hold)
    """
    if is_lte_retx_mcs(mcs):
        return (int(last_qm) if int(last_qm) in {2, 4, 6, 8} else 0), True
    return lte_nominal_qm_from_mcs(mcs), False


def qm_to_qam(qm: int) -> str:
    try:
        q = int(qm)
    except Exception:
        q = 0
    if q == 2:
        return "QPSK"
    if q == 4:
        return "16QAM"
    if q == 6:
        return "64QAM"
    if q == 8:
        return "256QAM"
    return "N/A"


def normalize_qam_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in {"QPSK", "16QAM", "64QAM", "256QAM", "N/A"}:
        return upper
    return text


def canonical_qam_label(value: object) -> str:
    """Normalize exported QAM fields to one public string contract.

    Raw modem Qm values remain available through qm_* / backend internals.
    Public qam_* fields should be labels ("QPSK", "16QAM", ...) or blank.
    """
    if value in (None, ""):
        return ""
    try:
        qm = int(str(value).strip())
    except Exception:
        qm = None
    if qm is not None:
        return qm_to_qam(qm) if qm in {2, 4, 6, 8} else ""
    label = normalize_qam_label(value)
    return "" if label in {"", "N/A"} else label


def canonical_qam_label_from_value_or_mcs(value: object, mcs: object) -> str:
    """Return canonical QAM label, deriving from LTE MCS when direct QAM is absent."""
    label = canonical_qam_label(value)
    if label:
        return label
    qm = lte_nominal_qm_from_mcs(mcs)
    return "" if qm not in {2, 4, 6, 8} else qm_to_qam(qm)


def cqi_to_nominal_qm(cqi: int) -> int:
    """Map CQI bucket to a nominal LTE modulation order Qm.

    This is intentionally coarse. We use it when a backend has CQI but no
    trustworthy scheduler/QAM evidence. The bucket edges align with the
    internal 4-lane modulation model rather than pretending we know the
    exact vendor MCS choice.
    """
    try:
        q = int(cqi)
    except Exception:
        return 0
    if q <= 0:
        return 0
    if q < 4:
        return 2
    if q < 8:
        return 4
    if q < 12:
        return 6
    return 8


def representative_cqi_for_qm(qm: int) -> int:
    """Return the representative CQI band-center for a QAM bucket."""
    try:
        q = int(qm)
    except Exception:
        return 0
    if q == 2:
        return 2
    if q == 4:
        return 6
    if q == 6:
        return 10
    if q == 8:
        return 14
    return 0


def estimate_qm_from_sinr(sinr_db: float) -> int:
    """Estimate LTE QAM bucket directly from SINR.

    This is a deliberate 4-bucket estimator for unsupported-CQI devices.
    It is easier to calibrate per modem family than a fake exact CQI ladder.
    The thresholds are the current family-neutral defaults and may be refined
    later by profile overlays or family-specific calibration.
    """
    try:
        sinr = float(sinr_db)
    except Exception:
        return 0
    if abs(sinr) < 0.001:
        return 0
    if sinr < 1.0:
        return 2
    if sinr < 8.5:
        return 4
    if sinr < 16.0:
        return 6
    return 8
