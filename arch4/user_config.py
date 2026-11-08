"""Retired mixed-domain configuration path.

The former ``UserConfig`` object combined Core defaults, NGUI hints,
Foundation operator/license settings and Intelligence priors in one mutable XML
file.  Every runtime caller has been migrated to a typed owner store.  This
module deliberately exposes none of the former API: an attempted old import
fails loudly and identifies the legal replacement boundary.
"""

from __future__ import annotations


_REPLACEMENTS = {
    "UserConfig": "arch4.external_settings_decoder plus typed owner stores",
    "save_last_modem": "arch4.ngui.settings_store.NGUISettingsStore",
    "save_connection_hint": "arch4.ngui.settings_store.NGUISettingsStore",
    "save_traffic_plan_settings": (
        "arch4.foundation.operator_settings.FoundationOperatorSettingsStore"
    ),
    "traffic_plan_scope_id": "arch4.customer_contracts.settings",
    "config_persistence_status": "arch4.foundation.settings_health",
    "save_license_to_config": "arch4.license_credential_store",
    "save_license_dat": "arch4.license_credential_store",
    "load_license_dat": "arch4.license_credential_store",
    "load_license_tier_dat": "arch4.license_credential_store",
    "CONFIG_FILENAME": "arch4.config_file_contract",
    "LICENSE_DAT_FILENAME": "arch4.license_credential_store",
    "is_scrubbed_connection_user": "arch4.customer_contracts.settings",
}


def __getattr__(name: str):
    replacement = _REPLACEMENTS.get(str(name))
    if replacement is None:
        raise AttributeError(name)
    raise RuntimeError(
        f"arch4.user_config.{name} is illegal; use {replacement}"
    )


__all__: tuple[str, ...] = ()
