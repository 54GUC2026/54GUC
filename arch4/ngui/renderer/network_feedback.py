"""Presentation of canonical diagnostic checks and network-command outcomes."""
from collections.abc import Mapping
from datetime import datetime
from arch4.core_contracts.network_diagnostics import network_diagnostic_from_matrix


NETWORK_NEXT_STEPS = {
    "sim_unavailable": "SIM identity is unavailable. Check that the SIM is inserted and enabled in the router, then retry Connect.",
    "sim_locked": "SIM is locked. Enter its PIN or PUK in the router's SIM settings, then retry Connect.",
    "data_disabled": "Mobile data is off. Keep the device connected to edit supported settings, then click Apply settings. Otherwise, use Mobile connect to reconnect.",
    "data_state_unavailable": "The router did not report its Mobile Data setting. Check it in the router's settings; enable it if off.",
    "manual_connection": "The router is set to wait for a manual connection request. Applying your choices with Connect also requests a mobile connection. If you are keeping the router's settings unchanged, use Connect in its own settings.",
    "connection_mode_unavailable": "Data connection mode could not be read. Check Automatic data connection in the router's settings.",
    "roaming_disabled": "Roaming is disabled. If this SIM needs roaming here, disconnect mobile data, enable Roaming and click Apply settings.",
    "roaming_state_unavailable": "Roaming permission could not be read. Check the router's roaming setting before retrying your selected network.",
    "apn_unavailable": "The active APN could not be read. Check the subscription's APN and active profile in the router's settings before retrying.",
    "apn_read_only": "The active APN is read-only. Use a separate user profile if a different APN is required.",
    "selection_unavailable": "Operator selection could not be read. Check manual selection and the intended network in the router's settings.",
    "registration_failed": "The router reports that its last attempt to join the selected network failed. Check that your SIM can use that network, then retry your selection.",
    "registration_pending": "The router is still trying to join the selected network. Wait for that attempt to finish before trying again.",
    "registration_result_unavailable": "The router did not provide a usable registration result. Check its network status or retry the selected network; this does not prove rejection.",
    "registered_without_data": "The last registration result reports success, but mobile service is still unavailable. Check Mobile Data, roaming permission and the subscription's APN.",
    "mobile_state_unavailable": "Mobile status is unavailable. Check the router's network status; export a support bundle if it stays unavailable.",
    "no_mobile_service": "No mobile service is reported. Check signal and the selected network. Retry with the saved settings; if necessary, use the router's normal restart after checking them.",
    "radio_disabled": "The cellular radio is off. Disable Airplane Mode in the router's settings. If its firmware cannot bring the radio online, use its normal restart and retain the selected network.",
    "radio_mode_unavailable": "Cellular radio mode could not be read. Check that Airplane Mode is off in the router's settings; this missing reply does not prove the radio is disabled.",
}

NETWORK_FAILURE_STEPS = {
    "cellular_mutation_inflight": "Another modem operation is still running. These changes were not sent. Your choices are kept. Wait for that operation to finish, then Disconnect and Connect to retry.",
    "network_settings_read_only": "SIM, network and APN settings are read-only in 54GUC for this device. Change them in the router's own settings.",
    "apn_explicit_password_required": "APN password required. Enter it, then Connect again.",
    "network_subscription_changed_or_unavailable": "The active SIM changed or could not be identified. Disconnect, check the SIM and its settings, then Connect. Saved settings for another SIM were not applied.",
    "network_active_manual_apn_required": "The active APN cannot be edited. Select a manual APN profile in the router, then retry with this form.",
    "network_apn_authentication_unsupported": "This router cannot apply that APN authentication type. Select None, PAP or CHAP as specified by the SIM provider.",
    "network_apn_credential_username_mismatch": "The APN username and saved credentials do not match. Re-enter the APN username and password, then Connect.",
    "network_operator_scan_rejected": "The router did not accept the network scan. Check its local network-selection page, then retry the selected network. Export a support bundle if scans remain unavailable.",
    "network_operator_scan_reply_unavailable": "The scan acknowledgement was not received. The router may still be scanning; check its network list before retrying. The registration command was not sent.",
    "network_operator_scan_timed_out": "The network scan did not finish in time. Let the router finish scanning, then retry the same network.",
    "network_target_scan_rat_unavailable_or_ambiguous": "The scan did not identify one usable radio technology for the selected network. Check the router's network list and select the intended network there, then reconnect 54GUC.",
    "network_stage_order_invalid": "The connection steps were rejected before writing. Export a support bundle so this software error can be checked.",
    "network_data_hold_not_confirmed": "The router did not confirm stopping mobile data. Further changes were stopped; check its Mobile Data setting before retrying.",
    "network_roaming_not_confirmed": "The router did not retain the approved roaming setting. Mobile data remains stopped; check its roaming control, then retry.",
    "network_apn_not_confirmed": "The router did not retain the approved APN. Mobile data remains stopped; check its active APN profile, then retry.",
    "tplink_apn_factory_profile_read_only": "The factory APN cannot be changed. Choose NEW to create a separate profile.",
    "tplink_apn_name_already_exists": "That APN profile name already exists. Choose a different name for the new profile.",
    "tplink_apn_creation_not_qualified": "Creating an APN profile is not available for this router firmware.",
    "network_selection_not_confirmed": "The router did not confirm the requested network-selection mode. Mobile data remains stopped; check its network settings, then retry.",
    "network_wwan_configuration_unavailable": "The router's data settings could not be read safely. Check Mobile Data and roaming in its settings, then retry.",
    "network_profile_contract_required": "This firmware's network-control interface is not qualified. Use the router's settings and export a support bundle for compatibility diagnosis.",
    "network_stage_unsupported": "The router cannot apply this setting through the available interface. Apply it in the router's settings, then reconnect 54GUC.",
    "network_command_rejected": "The router explicitly rejected the settings command. Review the network checks and its local settings, then retry the selected network.",
    "network_command_reply_unavailable": "The settings acknowledgement was not received. The router may have applied the command; check the readback and network checks before retrying.",
    "accepted_deadline_expired": "The router did not confirm all requested settings within the allowed time. A mobile connection alone does not confirm the selected network or APN. Compare your choices with the router readback below. To retry, click Disconnect, review the fields, then click Connect.",
}


NETWORK_STAGE_TEXT = {
    "network_data_hold": "Preparing the router for your changes by stopping mobile data. The connection to the router stays active.",
    "network_apn": "Applying your APN profile and checking the router's active profile.",
    "network_apn_create": "Creating your separate APN profile. The factory profile stays unchanged.",
    "network_roaming": "Applying your roaming choice.",
    "network_operator": "Selecting your requested network. For a specified network, the router scans first; this can take up to 30 seconds.",
    "network_data_start": "Checking that the router retained your settings, then requesting a mobile data connection.",
}


def network_progress_text(result, *, now):
    receipt = result.get("canonical_receipt") or {}
    progress = receipt.get("receipts", {}).get("network_progress", {})
    if not progress:
        return "Your settings are queued. Waiting for the router command to start; your choices are kept."
    stage = progress["stage_kind"]
    elapsed = max(0, int(now - progress["started_at"]))
    return (f"Step {progress['ordinal']} of {progress['total']}: {NETWORK_STAGE_TEXT[stage]}"
            f"\nWaiting for this step to finish: {elapsed} seconds.")


def network_failure_text(reason):
    # Combined stage failures retain the command owner's order, with no repeat.
    return " ".join(dict.fromkeys(NETWORK_FAILURE_STEPS.get(part, part)
                                  for part in str(reason).split(";") if part))


def network_diagnostic_text(envelope, *, subscription, mobile_status=""):
    if not isinstance(envelope, Mapping) or envelope.get("kind") != "network_connection_diagnostic":
        return ""
    diagnostic = network_diagnostic_from_matrix(envelope.get("data"))
    if diagnostic["subscription_key"] and diagnostic["subscription_key"] != subscription:
        return ""
    if diagnostic["mobile_connected"] is True or mobile_status == "Connected":
        return ""
    checked_at = datetime.fromtimestamp(envelope["observed_at"]).strftime("%H:%M:%S")
    service = (
        "The router is not connected to a mobile network."
        if diagnostic["mobile_connected"] is False else
        "The router has not reported whether it is connected to a mobile network."
    )
    # Present prerequisites before settings and the resulting connection attempt.
    order = (
        "sim_unavailable", "sim_locked", "radio_disabled", "radio_mode_unavailable",
        "roaming_disabled", "roaming_state_unavailable", "apn_unavailable", "apn_read_only",
        "selection_unavailable", "data_disabled", "data_state_unavailable",
        "manual_connection", "connection_mode_unavailable", "registration_failed",
        "registration_pending", "registration_result_unavailable", "registered_without_data",
        "mobile_state_unavailable", "no_mobile_service",
    )
    issues = [issue for issue in order if issue in diagnostic["issues"]]
    if len(issues) > 1 and "no_mobile_service" in issues:
        issues.remove("no_mobile_service")  # Already explained above; use the specific findings.
    steps = "\n\n".join(f"{index}. {NETWORK_NEXT_STEPS[issue]}" for index, issue in enumerate(issues, 1))
    return f"54GUC is connected to your router.\n{service}\n\nLast checks: {checked_at}.\n\n{steps}"
