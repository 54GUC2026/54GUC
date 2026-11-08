"""Versioned service-quality coordinates; device class never follows live RAT."""

LTE_QUALITY_MODEL = "lte-bq-cq-v1"
NR_QUALITY_MODEL = "5g-combined-v1"
UNKNOWN_QUALITY_MODEL = "unclassified"


def require_quality_model(value: str) -> str:
    if type(value) is not str or value not in {LTE_QUALITY_MODEL, NR_QUALITY_MODEL, UNKNOWN_QUALITY_MODEL}:
        raise ValueError("quality_model_contract_invalid")
    return value


def quality_model_for_device_class(device_class: str) -> str:
    if device_class == "5g":
        return NR_QUALITY_MODEL
    if device_class == "4g_only":
        return LTE_QUALITY_MODEL
    if device_class == "unclassified":
        return UNKNOWN_QUALITY_MODEL
    raise ValueError("device_radio_class_contract_invalid")
