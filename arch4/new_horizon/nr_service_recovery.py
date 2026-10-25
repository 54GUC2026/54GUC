"""Brain's bounded NSA health evidence, using existing accepted STM contexts.

No transport, timer, cache, learned score or Internet interpretation lives here.
Core's existing service timeline supplies NET truth. A mode command is a
separate, capability-qualified decision after this evidence has been admitted.
"""
from dataclasses import dataclass
import math

from arch4.core_contracts.action_reachability import (
    CoreReachabilityEvidenceView, core_reachability_interval_receipt,
)
from arch4.new_horizon.contracts import IntelligenceContext
from arch4.core_contracts.command_targets import (
    CoreRadioModeTarget, core_command_target_from_payload, validate_radio_mode_observation,
)
from arch4.core_contracts.intelligence_boundary import INTELLIGENCE_CONTROL_AUTOPILOT
from arch4.runtime_capability import capability_contract_action_meta


NR_FAILURE_WINDOW_S = 20.0
NR_FAILURE_SINR_DB = 3.0
NR_FAILURE_RTT_MS = 150.0
NR_FAILURE_JITTER_MS = 50.0
NR_RECOVERY_SINR_DB = 5.0
NR_RECOVERY_RTT_MS = 120.0
NR_RECOVERY_JITTER_MS = 20.0
NR_RECOVERY_STABLE_S = 120.0
# All current profiles sample RF every 2.0 or 2.1s. Missing coverage fails closed;
# this is an evidence allowance, never a new sampling cadence or a held sample.
MAX_RF_GAP_S = 6.0
NR_RETRY_DELAYS_S = (1800.0, 3600.0, 5400.0)


@dataclass(frozen=True, slots=True)
class NrServiceLossEvidence:
    started_at: float
    ended_at: float
    bad_sample_count: int
    first_half_bad: int
    second_half_bad: int
    service_signs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def _number(value):
    return float(value) if type(value) in {int, float} and math.isfinite(value) else None


def _fact(context, path, *, fresh=False):
    try:
        fact = context.fact(path)
    except KeyError:
        return None
    if fact.disposition != "available":
        return None
    if fresh and (fact.retention is not None or not fact.acquisition_attempt_id
                  or fact.acquisition_namespace != context.matrix_identity.namespace
                  or not 0 < fact.sampled_at <= fact.acquired_at <= context.accepted_at
                  or context.accepted_at - fact.sampled_at > MAX_RF_GAP_S):
        return None
    return fact.value.get("value")


def _scope(context):
    paths = ("identity.profile_id", "identity.provider_plmn", "radio.nr_band",
             "radio.nr_arfcn", "radio.nr_pci", "rf.cell_id", "rf.earfcn_dl", "rf.pci")
    values = tuple(_fact(context, path) for path in paths)
    if any(value is None for value in values):
        return None
    mode = _fact(context, "slow.radio_mode")
    mode_scope = (None if not hasattr(mode, "get") else
        (mode.get("subscription_key"), mode.get("profile_definition_hash"), mode.get("mode"),
         mode.get("band_selection_mode"), tuple(mode.get("lte_bands", ())), tuple(mode.get("nr_bands", ()))))
    if mode_scope is not None:
        mode_scope += (mode.get("mode_key"), mode.get("lte_selection_mode"),
            mode.get("nr_selection_mode"), mode.get("preserved_configuration_key"))
    return (context.matrix_identity.namespace, context.identity_code,
            context.confirmed_continuity.get("learning_subscription_key"), mode_scope, *values)


def _rf(context):
    if (_fact(context, "radio.network_type", fresh=True) != "NR NSA (5G)"
            or _fact(context, "radio.nr5g_active", fresh=True) is not True):
        return None
    sinr = _number(_fact(context, "radio.nr_sinr_db", fresh=True))
    if sinr is None or not -30 <= sinr <= 50:
        return None
    fact = context.fact("radio.nr_sinr_db")
    return fact.sampled_at, sinr, (fact.acquisition_attempt_id, fact.acquired_at)


def _latency(context):
    surface = context.surface("latency")
    if not surface.valid or surface.stale:
        return None, None
    value = surface.value
    observation = value.get("rtt_observation")
    if (not hasattr(observation, "get") or observation.get("probe_status") != "ok"
            or observation.get("freshness") != "fresh"):
        return None, None
    stamp = _number(observation.get("observed_at"))
    if stamp is None or not 0 <= context.accepted_at - stamp <= MAX_RF_GAP_S:
        return None, None
    rtt = _number(value.get("rtt_sliding_avg"))
    jitter = _number(value.get("rtt_dl_jitter_trimmed_ms")) if value.get("rtt_dl_jitter_valid") is True else None
    return rtt if rtt is not None and rtt >= 0 else None, jitter if jitter is not None and jitter >= 0 else None


def _service_signs(current, samples, start, end):
    surface = current.surface("reachability_evidence")
    if not surface.valid or surface.stale:
        return ()
    signs = set()
    evidence = CoreReachabilityEvidenceView.from_mapping(surface.value.to_mapping())
    if (evidence.session_scope_key != current.matrix_identity.session_scope_key
            or evidence.identity_code != current.identity_code):
        return ()
    receipt = core_reachability_interval_receipt(evidence,
        interval_started_at=start, interval_ended_at=end, complete=True)
    # Core excludes planned pauses and unobserved portions. Require its
    # full-window coverage, including before accepting latency disturbance.
    if not math.isclose(receipt.measured_seconds, end - start, abs_tol=1e-6):
        return ()
    if receipt.availability_pct is not None and receipt.availability_pct < 100.0:
        signs.add("core_net_loss")
    for context in samples:
        if not start <= context.accepted_at <= end:
            continue
        rtt, jitter = _latency(context)
        if rtt is not None and rtt >= NR_FAILURE_RTT_MS:
            signs.add("rtt")
        if jitter is not None and jitter >= NR_FAILURE_JITTER_MS:
            signs.add("jitter")
    return tuple(sorted(signs))


def assess_nr_service_loss(contexts: tuple[IntelligenceContext, ...]) -> NrServiceLossEvidence | None:
    """Require a complete 20s NSA window; isolated RF or ping noise cannot act.

    Callers retain ordinary Brain/manual/capability guards. This function only
    reads the existing bounded STM and the existing Core NET evidence surface.
    """
    if not contexts:
        return None
    current = contexts[-1]
    if not all(isinstance(context, IntelligenceContext) for context in contexts):
        raise TypeError("nr_recovery_accepted_context_required")
    scope = _scope(current)
    anchor_sinr = _number(_fact(current, "rf.sinr_db", fresh=True))
    if (scope is None or _rf(current) is None
            or _fact(current, "health.radio_connected", fresh=True) is not True
            or anchor_sinr is None or anchor_sinr < 3.0):
        return None
    rows = []
    seen = set()
    # Only the uninterrupted tail for this device/SIM/provider/cell may count.
    for context in reversed(contexts):
        if (_scope(context) != scope or _fact(context, "radio.network_type") != "NR NSA (5G)"
                or _fact(context, "radio.nr5g_active") is not True):
            break
        rf = _rf(context)
        if rf is None:
            continue  # A maintenance/traffic root is not another RF sample.
        stamp, sinr, receipt = rf
        if receipt not in seen:
            rows.append((stamp, sinr, context))
            seen.add(receipt)
    rows.reverse()
    now = current.accepted_at
    for start, initial_sinr, _context in rows:
        end = start + NR_FAILURE_WINDOW_S
        if initial_sinr >= NR_FAILURE_SINR_DB or not 0 <= now - end <= MAX_RF_GAP_S:
            continue
        window = [(stamp, sinr, context) for stamp, sinr, context in rows if start <= stamp <= end]
        stamps = [stamp for stamp, _sinr, _context in window]
        if (not stamps or end - stamps[-1] > MAX_RF_GAP_S
                or any(right - left > MAX_RF_GAP_S or right <= left for left, right in zip(stamps, stamps[1:]))):
            continue
        first = sum(sinr < NR_FAILURE_SINR_DB for stamp, sinr, _context in window if stamp < start + 10.0)
        second = sum(sinr < NR_FAILURE_SINR_DB for stamp, sinr, _context in window if stamp >= start + 10.0)
        if first < 2 or second < 2:
            continue
        signs = _service_signs(current, contexts, start, end)
        if signs:
            return NrServiceLossEvidence(start, end, first + second, first, second, signs,
                tuple("nr-rf:" + context.root_fingerprint for _stamp, _sinr, context in window))
    return None


def nr_recovery_sample_good(context: IntelligenceContext) -> bool:
    """A fresh good NR sample; LTE-only silence is never NR recovery proof."""
    rf = _rf(context)
    rtt, jitter = _latency(context)
    return bool(rf is not None and rf[1] >= NR_RECOVERY_SINR_DB
                and rtt is not None and rtt <= NR_RECOVERY_RTT_MS
                and jitter is not None and jitter <= NR_RECOVERY_JITTER_MS)


@dataclass(frozen=True, slots=True)
class NrRecoveryDecision:
    target: CoreRadioModeTarget | None
    reason: str
    evidence_refs: tuple[str, ...] = ()


def _net_up(context):
    view = context.surface("reachability")
    return view.valid and not view.stale and view.value.get("disposition") == "reachable"


class NrServiceRecovery:
    """A Brain rule, not another controller. Core owns dispatched-mode leases.

    Only the uninterrupted good-RF timer is volatile. A restart must earn the
    full120s again; it cannot manufacture recovery from an old saved timer.
    """

    def __init__(self):
        self._trial_root = ""
        self._good_scope = None
        self._good_since = None
        self._last_good_at = None
        self.trial_recovered = False

    def _trial_sample(self, context, root):
        scope = _scope(context)
        if root != self._trial_root:
            self.trial_recovered = False
        if root != self._trial_root or scope != self._good_scope:
            self._trial_root, self._good_scope = root, scope
            self._good_since = self._last_good_at = None
        rtt, jitter = _latency(context)
        if (_net_up(context) and rtt is not None and rtt <= NR_RECOVERY_RTT_MS
                and jitter is not None and jitter <= NR_RECOVERY_JITTER_MS):
            fact = context.fact("radio.nr_sinr_db")
            if (fact.disposition == "available" and fact.retention is not None
                    and self._last_good_at is not None
                    and 0 <= context.accepted_at - self._last_good_at <= MAX_RF_GAP_S):
                return  # No new RF sample; do not advance or erase its timer.
        if not nr_recovery_sample_good(context) or not _net_up(context):
            self._good_since = self._last_good_at = None
            return
        stamp = _rf(context)[0]
        if self._last_good_at is not None and stamp <= self._last_good_at:
            return
        if self._last_good_at is None or stamp - self._last_good_at > MAX_RF_GAP_S:
            self._good_since = stamp
        self._last_good_at = stamp
        if stamp - self._good_since >= NR_RECOVERY_STABLE_S:
            self.trial_recovered = True

    def status(self, contexts: tuple[IntelligenceContext, ...]) -> dict:
        """Explain observed recovery separately from permission to command.

        This derives one presentation from existing STM/lease facts. It neither
        changes recovery state nor suppresses ordinary LTE/Brain decisions.
        """
        def result(state, message, *, warning=False, retry_at=0.0):
            return dict(state=state, message=message, warning=warning, retry_at=retry_at)
        if not contexts:
            return result("inactive", "")
        current = contexts[-1]
        mutation = current.surface("mutation")
        sources = (mutation.value.get("sources") or {}) if mutation.valid and not mutation.stale else {}
        lease = sources.get("radio_mode_lease") or {}
        state = lease.get("state", "inactive")
        guards = sources.get("band_action_policy") or {}
        network = current.surface("reachability")
        emergency = current.surface("emergency_reachability")
        if ((network.valid and not network.stale and network.value.get("disposition") == "unreachable")
                or (emergency.valid and not emergency.stale and emergency.value.get("confirmed") is True)
                or guards.get("emergency_recovery_hold_active")):
            message = "Recovering 4G/Internet service first."
            if state in {"pending", "holding_lte", "trial", "suspended"}:
                message += " 5G retries wait until LTE recovery is complete and service is stable."
            return result("lte_recovery", message, warning=True)
        if state == "pending":
            return result("mode_changing", "Changing radio mode; waiting for the router's response and fresh setting confirmation.")
        if state == "suspended":
            if lease.get("terminal_reason") == "nr_settings_changed_after_lte_rescue":
                return result("manual_action", "5G retries are paused: the NR settings no longer match the saved 5G settings after LTE recovery. Review the router's settings in its WebUI.", warning=True)
            return result("manual_action", "The radio-mode result is unconfirmed; automatic 5G retries are stopped. Check the router's mode in its WebUI.", warning=True)
        raw_mode = _fact(current, "slow.radio_mode")
        mode = raw_mode.get("mode") if hasattr(raw_mode, "get") else None
        if state == "holding_lte":
            retry_at = _number(lease.get("retry_at")) or 0.0
            if current.accepted_at < retry_at:
                return result("lte_hold", "Using 4G temporarily after poor 5G service. The saved 5G settings will be retried when due and LTE is stable.", retry_at=retry_at)
            rtt, jitter = _latency(current)
            anchor = _number(_fact(current, "rf.sinr_db", fresh=True))
            if (not _net_up(current) or _fact(current, "health.radio_connected", fresh=True) is not True
                    or anchor is None or anchor < 3.0 or rtt is None or rtt > NR_RECOVERY_RTT_MS
                    or jitter is None or jitter > NR_RECOVERY_JITTER_MS or guards.get("radio_stability_hold_active")):
                return result("waiting_for_lte", "The 5G retry is due, but it is waiting for stable 4G service and the current recovery hold to clear.", retry_at=retry_at)
            return result("nr_retry_due", "4G service is stable and the 5G retry is due. The saved 5G mode and bands can resume when automatic control is ready.", retry_at=retry_at)
        if state == "trial":
            if self.trial_recovered:
                return result("nr_recovered", "5G recovered: fresh radio readings and Internet service stayed good for 120 seconds.")
            return result("nr_trial", "5G is enabled for a recovery trial. Waiting for NR attachment and 120 seconds of good fresh radio and service readings; LTE continues to carry available service.")
        loss = assess_nr_service_loss(contexts)
        if loss is not None:
            capabilities = _fact(current, "capability.runtime_capabilities")
            capability = capability_contract_action_meta(capabilities, "radio_mode") or {}
            if (mode != "nsa_preferred" or capability.get("state") != "supported"
                    or capability.get("write_allowed") is not True):
                return result("manual_action", "Poor 5G signal is affecting service. Automatic mode control is unavailable for this profile; use the router WebUI to select 4G-only if needed. Monitoring continues.", warning=True)
            return result("nr_degraded", "Poor 5G signal is affecting service. Temporary 4G fallback is available when automatic control and the existing radio-action guards permit it.", warning=True)
        if mode == "lte_only":
            return result("lte_selected", "The router is set to 4G-only; 54GUC will keep that setting. Use Manual NR or the router WebUI when you want to enable 5G.")
        if mode == "nsa_preferred" or _fact(current, "radio.network_type") == "NR NSA (5G)":
            return result("monitoring", "Monitoring 5G and LTE service; the router's current mode and band settings are preserved.")
        return result("inactive", "")

    def evaluate(self, contexts: tuple[IntelligenceContext, ...]) -> NrRecoveryDecision | None:
        if not contexts:
            return None
        current = contexts[-1]
        control = current.product_control
        if (control.get("control_mode") != INTELLIGENCE_CONTROL_AUTOPILOT
                or control.get("control_available") is not True
                or control.get("connected_settled") is not True):
            return None
        raw_mode = _fact(current, "slow.radio_mode")
        if raw_mode is None:
            return None
        capabilities = _fact(current, "capability.runtime_capabilities")
        mode_capability = capability_contract_action_meta(capabilities, "radio_mode") or {}
        if mode_capability.get("write_allowed") is not True or mode_capability.get("state") != "supported":
            return None
        mutation = current.surface("mutation")
        if not mutation.valid or mutation.stale or mutation.value.get("active") is True:
            return None
        sources = mutation.value.get("sources") or {}
        guards = sources.get("band_action_policy") or {}
        if guards.get("operator_cooldown_active") or guards.get("emergency_recovery_hold_active"):
            return None
        configuration = validate_radio_mode_observation(raw_mode)
        lease = sources.get("radio_mode_lease") or {"state": "inactive"}
        state = lease.get("state")
        if state in {"pending", "suspended"}:
            return NrRecoveryDecision(None, "nr_mode_command_pending" if state == "pending" else "nr_mode_command_unconfirmed_manual_review")
        prior = None
        if state in {"holding_lte", "trial"}:
            if lease.get("identity_key") != current.matrix_identity.session_scope_key:
                return NrRecoveryDecision(None, "nr_mode_lease_device_changed")
            prior = core_command_target_from_payload(lease["target"])
            if not isinstance(prior, CoreRadioModeTarget):
                raise TypeError("nr_recovery_typed_mode_lease_required")
            expected = prior.configuration()
            if expected != configuration:
                return NrRecoveryDecision(None, "nr_mode_lease_configuration_changed")
        if state == "holding_lte":
            anchor = _number(_fact(current, "rf.sinr_db", fresh=True))
            if _fact(current, "health.radio_connected", fresh=True) is not True or anchor is None or anchor < 3:
                return None  # Leave existing LTE/Internet emergency policy in charge.
            rtt, jitter = _latency(current)
            if current.accepted_at < lease["retry_at"]:
                return NrRecoveryDecision(None, "nr_mode_lte_recovery_hold")
            if (guards.get("radio_stability_hold_active") or not _net_up(current)
                    or rtt is None or rtt > NR_RECOVERY_RTT_MS
                    or jitter is None or jitter > NR_RECOVERY_JITTER_MS):
                return NrRecoveryDecision(None, "nr_mode_waiting_for_stable_lte_service")
            target = CoreRadioModeTarget.transition(configuration, mode="nsa_preferred",
                mode_key=prior.expected_mode_key, retry_delay_s=prior.retry_delay_s)
            return NrRecoveryDecision(target, "nr_mode_scheduled_mixed_trial",
                ("core-mode-lease:" + lease["root_id"], "mode-configuration:" + current.root_fingerprint))
        if (configuration["mode"] != "nsa_preferred"
                or (not configuration["nr_bands"] and configuration.get("nr_selection_mode") != "auto")):
            return None  # Keep unowned LTE-only or an empty NR selection unchanged.
        if state == "trial":
            self._trial_sample(current, lease["root_id"])
        loss = assess_nr_service_loss(contexts)
        if loss is None:
            return None  # Normal Brain/LTE policy remains available during a mixed-mode trial.
        delay = NR_RETRY_DELAYS_S[0]
        if state == "trial" and not self.trial_recovered:
            delay = NR_RETRY_DELAYS_S[min(2, NR_RETRY_DELAYS_S.index(float(prior.retry_delay_s)) + 1)]
        target = CoreRadioModeTarget.transition(configuration, mode="lte_only", retry_delay_s=delay)
        return NrRecoveryDecision(target, "nr_service_loss_confirmed", loss.evidence_refs)
