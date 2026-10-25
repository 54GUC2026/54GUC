"""Stateless selected-profile translation for configured radio preferences.

Native mode values stay in Modem Live.  Core keeps a finite profile choice and
the settings which a temporary LTE fallback must preserve, never a wire command.
"""
from copy import deepcopy
import hashlib
import json
import re
from collections.abc import Mapping

from arch4.core_contracts.command_targets import (
    CoreRadioModeTarget,
    validate_radio_mode_observation,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def profile_radio_modes(definition):
    """Read only the finite choice codec of the selected immutable profile."""
    document = definition.snapshot()
    modes = document.get("parameters", {}).get("radio_modes")
    if (not isinstance(modes, dict) or not 2 <= len(modes) <= 32
            or "lte_only" not in modes
            or any(not isinstance(key, str) or
                   re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None for key in modes)):
        raise RuntimeError("radio_mode_profile_choices_unavailable")
    try:
        encoded = [_canonical(value) for value in modes.values()]
    except (TypeError, ValueError, RecursionError):
        raise RuntimeError("radio_mode_profile_choices_invalid") from None
    if (len(set(encoded)) != len(encoded)
            or any(value is None or isinstance(value, (bool, int, float))
                   for value in modes.values())
            or any(len(value) > 4096 for value in encoded)):
        raise RuntimeError("radio_mode_profile_choices_invalid")
    return deepcopy(modes)


def mode_key_for_native(definition, native):
    encoded = _canonical(native)
    for key, value in profile_radio_modes(definition).items():
        if _canonical(value) == encoded:
            return key
    raise RuntimeError("radio_mode_native_preference_unrecognized")


def native_mode_for_key(definition, key):
    modes = profile_radio_modes(definition)
    if not isinstance(key, str) or key not in modes:
        raise RuntimeError("radio_mode_profile_choice_unsupported")
    return deepcopy(modes[key])


def preserved_configuration_key(configuration):
    if not isinstance(configuration, Mapping) or not configuration:
        raise RuntimeError("radio_mode_preserved_configuration_required")
    return hashlib.sha256(_canonical(dict(configuration)).encode("utf-8")).hexdigest()


def radio_mode_observation(definition, *, mode_key, lte_bands, nr_bands,
                           lte_selection_mode, nr_selection_mode,
                           subscription_key, preserved_configuration):
    native_mode_for_key(definition, mode_key)
    return validate_radio_mode_observation({
        "mode": "lte_only" if mode_key == "lte_only" else "nsa_preferred",
        "mode_key": mode_key,
        "lte_bands": list(lte_bands), "nr_bands": list(nr_bands),
        "lte_selection_mode": lte_selection_mode,
        "nr_selection_mode": nr_selection_mode,
        "band_selection_mode": ("auto" if lte_selection_mode == nr_selection_mode == "auto"
                                else "manual"),
        "subscription_key": subscription_key,
        "profile_definition_hash": definition.content_hash,
        "preserved_configuration_key": preserved_configuration_key(preserved_configuration),
    })


def require_radio_mode_configuration(target, observation):
    if not isinstance(target, CoreRadioModeTarget):
        raise TypeError("radio_mode_typed_target_required")
    try:
        current = validate_radio_mode_observation(observation)
    except (TypeError, ValueError):
        raise RuntimeError("radio_mode_original_configuration_changed") from None
    if current != target.configuration(expected=True):
        raise RuntimeError("radio_mode_original_configuration_changed")
