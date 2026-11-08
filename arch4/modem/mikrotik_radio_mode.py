"""Stateless RouterOS configured-RAT read and guarded native write.

The binary API already consumes !done and raises for !trap. No AT mode owner,
registration inference, supported-band substitution or retained mode is used.
"""
import re
import time

from arch4.core_contracts.network_settings import subscription_key
from arch4.modem_live.radio_mode_configuration import (
    mode_key_for_native, native_mode_for_key, profile_radio_modes, radio_mode_observation,
    require_radio_mode_configuration,
)
from arch4.modem_live.physical_command import ModemLivePhysicalStageResult
from arch4.modem.routeros_request_adapter import RouterOsProtocolError, RouterOsTransportError


def qualified(modem):
    definition = getattr(modem, "_definition", None)
    return bool(definition is not None
        and definition.metadata["capabilities"].get("supports_radio_mode") is True)


def _selected(source, names):
    # Missing is not the explicitly empty AUTO/operator/APN setting.
    return {key: {"present": key in source, "value": source.get(key)} for key in names}


def _require_mode_semantics(definition):
    modes = profile_radio_modes(definition)
    if modes["lte_only"] != "lte":
        raise ValueError("mikrotik_radio_mode_lte_only_invalid")
    for key, value in modes.items():
        if key == "lte_only":
            continue
        if type(value) is not str:
            raise ValueError("mikrotik_radio_mode_enabled_invalid")
        rats = value.split(",")
        if (len(rats) != len(set(rats)) or not {"lte", "5g"} <= set(rats)
                or not set(rats) <= {"3g", "lte", "5g"}):
            raise ValueError("mikrotik_radio_mode_enabled_invalid")


def _restriction(row, key):
    if key not in row or type(row[key]) is not str:
        raise ValueError("native_restriction_unavailable")
    text = row[key]
    if text == "":
        return [], "auto"
    if re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", text) is None:
        raise ValueError("native_restriction_invalid")
    values = [int(value) for value in text.split(",")]
    if len(set(values)) != len(values):
        raise ValueError("native_restriction_duplicate")
    return sorted(values), "manual"


def observations(modem, lane, *, required=False):
    """Each maintenance yield releases the existing outer I/O lock.

    Locals belong to this one acquisition, never a retained configuration cache.
    Event-write/readback callers drain the same generator under their own lock.
    """
    if not qualified(modem):
        raise RuntimeError("mikrotik_selected_profile_radio_mode_not_qualified")
    try:
        _require_mode_semantics(modem._definition)
        if modem._routeros is None or not modem._lte_id:
            raise ValueError("selected_interface_unavailable")
        with modem._physical_request_authority.lane_scope(lane):
            resource = modem._routeros.get_resource("/interface/lte")
            rows = resource.get()
        yield None
        matches = [row for row in rows if isinstance(row, dict)
                   and modem._routeros_row_id(row) == modem._lte_id]
        if len(matches) != 1:
            raise ValueError("selected_interface_unavailable")
        row = matches[0]
        if not row.get("name"):
            raise ValueError("selected_interface_name_unavailable")
        mode_key = mode_key_for_native(modem._definition, row.get("network-mode"))
        lte, lte_mode = _restriction(row, "band")
        nr, nr_mode = _restriction(row, "nr-band")
        with modem._physical_request_authority.lane_scope(lane):
            monitor = modem._routeros.get_binary_resource("/interface/lte").call(
                "monitor", {"once": "", ".id": modem._lte_id})
        yield None
        if not isinstance(monitor, list) or len(monitor) != 1:
            raise ValueError("fresh_subscription_unavailable")
        from arch4.modem.mikrotik import _decode_row
        identity = _decode_row(monitor[0])
        subscription = subscription_key(str(identity.get("imsi") or ""))
        if not subscription:
            raise ValueError("fresh_subscription_unavailable")
        with modem._physical_request_authority.lane_scope(lane):
            resources = modem._routeros.get_resource("/system/resource").get()
        yield None
        with modem._physical_request_authority.lane_scope(lane):
            boards = modem._routeros.get_resource("/system/routerboard").get()
        yield None
        if len(resources) != 1 or len(boards) != 1:
            raise ValueError("fresh_device_identity_ambiguous")
        resource, board = resources[0], boards[0]
        if any(not str(value or "").strip() for value in (
                identity.get("model"), identity.get("revision"), resource.get("version"),
                board.get("serial-number"))):
            raise ValueError("fresh_device_identity_unavailable")
        native_identity = {"backend":"mikrotik", "vendor":"MikroTik",
            "model":" ".join(str(value) for value in (board.get("model"), identity["model"]) if value),
            "firmware":str(identity["revision"]), "serial":str(board["serial-number"]),
            "imei":str(identity.get("imei") or ""), "os_name":"RouterOS",
            "os_version":str(resource["version"]), "software_version":str(resource["version"]),
            "hardware_version":str(resource.get("architecture-name") or "")}
        from arch4.modem_live.device_admission import require_selected_device_admission
        require_selected_device_admission(backend="mikrotik",
            selected_profile_id=modem._definition.profile_id,
            observed=native_identity, associations=())
        admit = getattr(modem, "_startup_device_admission", None)
        if callable(admit):
            admit(native_identity)
        pointer = row.get("apn-profiles", row.get("apn-profile"))
        if type(pointer) is not str or not pointer:
            raise ValueError("selected_apn_unavailable")
        names = pointer.split(",")
        with modem._physical_request_authority.lane_scope(lane):
            apns = modem._routeros.get_resource("/interface/lte/apn").get()
        yield None
        selected = [item for item in apns if item.get("name") in names]
        if len(selected) != len(names) or {item.get("name") for item in selected} != set(names):
            raise ValueError("selected_apn_ambiguous")
        preserved = {
            "interface": _selected(row, (".id", "id", "name", "master", "apn-profiles",
                "apn-profile", "operator", "allow-roaming", "disabled", "modem-init", "pin")),
            "apn": sorted(selected, key=lambda item: str(item.get("name"))),
            "device": native_identity,
        }
        observed = radio_mode_observation(modem._definition, mode_key=mode_key,
            lte_bands=lte, nr_bands=nr, lte_selection_mode=lte_mode,
            nr_selection_mode=nr_mode, subscription_key=subscription,
            preserved_configuration=preserved)
        yield {"radio_mode": observed}
    except (TypeError, ValueError, KeyError, RuntimeError, ConnectionError) as exc:
        if required:
            raise RuntimeError("mikrotik_radio_mode_observation_unavailable") from exc
        yield {"radio_mode": None}


def status(modem, lane, *, required=False):
    return next(value for value in observations(modem, lane, required=required) if value is not None)


def execute(modem, plan):
    if plan.stage_kind != "radio_mode" or plan.ordinal != 1:
        raise ValueError("mikrotik_radio_mode_stage_invalid")
    observed = status(modem, "event_write", required=True)["radio_mode"]
    require_radio_mode_configuration(plan.target, observed)
    native = native_mode_for_key(modem._definition, plan.target.mode_key)
    payload = modem._definition.prepare_native_write_fields(
        "routeros:lte.network-mode", {"network_mode": native})
    if set(payload) != {"network_mode"} or payload["network_mode"] != native:
        raise RuntimeError("mikrotik_radio_mode_definition_mismatch")
    started = time.time()
    outcome, reason = "accepted", "accepted"
    try:
        modem._routeros.get_resource("/interface/lte").set(
            id=modem._lte_id, **{"network-mode": payload["network_mode"]})
    except RouterOsTransportError:
        outcome, reason = "response_unavailable", "mikrotik_radio_mode_reply_unavailable"
    except RouterOsProtocolError as exc:
        # Parsing failure does not prove that the modem refused the write.
        uncertain = type(exc.__cause__).__name__ == "RouterOsApiParsingError"
        outcome = "response_unavailable" if uncertain else "rejected"
        reason = "mikrotik_radio_mode_reply_unavailable" if uncertain else "mikrotik_radio_mode_rejected"
    returned = time.time()
    if outcome != "rejected":
        modem._invalidate_radio_caches()
        modem._begin_post_write_blackout(reason=plan.stage_kind)
    return ModemLivePhysicalStageResult(plan=plan, request_dispatched_at=started,
        transport_returned_at=returned, outcome=outcome, reason=reason,
        detail={"transport": "routeros_lte_network_mode_property"})
