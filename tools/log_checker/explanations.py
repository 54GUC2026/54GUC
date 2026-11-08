"""Offline customer wording. No diagnosis, measurements or live actions here."""

# Each detector names its explanation explicitly. Do not guess a diagnosis by
# matching free text, and do not let changed technical wording remove guidance.
FINDINGS = {
    "accepted_fields_unreadable": (
        "The complete input inventory could not be checked",
        "The saved accepted-field inventory is missing or failed its owner's validation.",
        "The checker cannot list every available, missing, invalid or unsupported field from that snapshot.",
        "Keep the original report and bundle. Older files may lack this inventory; no missing values are guessed."),
    "field_values_missing": (
        "Some saved input fields were unavailable",
        "The app explicitly recorded missing or invalid fields. Their exact names and recorded reasons appear in Saved input availability.",
        "This is an availability limit, not a count of software bugs. An absent radio cell, an unsupported field and a failed conversion have different meanings.",
        "Check the named fields and reasons against the symptom you are investigating. No repair is needed merely because an optional value is unavailable."),
    "quality_receipts_missing": (
        "Some quality inputs were not retained",
        "Some observations do not contain the saved inputs used for their quality calculations.",
        "The checker can show retained quality results but cannot independently trace every input. Older logs may not contain these receipts.",
        "Keep the original files. Do not replace missing inputs with current readings or infer an incorrect score from missing evidence alone."),
    "device_class_conflict": (
        "The saved router information disagrees",
        "Records for the selected connection describe it as both a 4G-only device and a 5G-capable device.",
        "The checker kept their learning records separate rather than mixing two incompatible descriptions.",
        "Send this report and the original support ZIP to support so the conflicting records can be traced."),
    "runtime_faults": (
        "The app recorded an internal error",
        "54GUC saved one or more runtime faults. A later successful recovery does not remove the earlier fault.",
        "Part of the app may have failed temporarily. The saved fault count alone does not identify the cause or duration.",
        "Keep the support ZIP and note what you noticed at the listed time. Send both with this report."),
    "worker_degraded": (
        "Part of the app was not working normally",
        "The last saved health check reports a failed or degraded worker, a task that handles part of the app's work.",
        "Some readings or decisions may have been delayed or unavailable. Ordinary worker stops during a clean shutdown are handled separately.",
        "Check the app and shutdown entries at the same time. Send the bundle if this happened during normal operation."),
    "shutdown_missing": (
        "The end of the run was not recorded",
        "There is no saved receipt confirming that the app finished closing.",
        "The bundle may have been exported while the app was still running. This entry alone is not a crash report.",
        "If you exported while running, no repair is needed for this item. To check shutdown, close 54GUC normally and review its saved folder afterwards."),
    "modem_timing_missing": (
        "Reading times could not be checked",
        "The saved files do not include the modem-reading timing snapshot.",
        "The checker cannot describe how fresh the last readings were or whether reads were missed.",
        "Keep the current report. Include a fresh support ZIP if you are reporting delayed or missing readings."),
    "learning_snapshot_missing": (
        "The learning snapshot is unavailable",
        "The files do not contain the summary of what Brain had learned at that time.",
        "Brain may have been switched off, or this export may not contain the snapshot. Neither is established as a fault here.",
        "Check whether Intelligence was enabled for the run. If it was, include the original support ZIP when asking about missing learning."),
    "device_class_missing": (
        "The saved device type could not be verified",
        "The saved capability record is missing or cannot be validated as a 4G-only or 5G-capable device.",
        "A 5G router can legitimately be using LTE. The checker therefore does not decide the device type from the current connection alone.",
        "Check that the intended modem profile was selected. Send the bundle if this repeats with the correct profile."),
    "duplicate_events": (
        "Some event entries look repeated",
        "Several entries share the same event type, time and transaction.",
        "They may be separate stages of one action. Repeated log entries do not establish that the router received duplicate commands.",
        "Compare the command stages and outcomes in this report. Send the bundle if the same user-visible action also happened more than once."),
    "evaluation_limit": (
        "There are more Brain checks than this report can include",
        "The export reached its limit for saved Brain evaluations, including decisions to keep the connection unchanged.",
        "Some earlier evaluations may be absent from this report.",
        "Keep the original files. Use a support ZIP covering the shorter period of interest when investigating a specific incident."),
    "command_parent_missing": (
        "A command result is missing its earlier decision",
        "A saved action result could not be joined to the Brain decision that requested it within this review window.",
        "The result can be listed, but its complete decision-to-result story cannot be reconstructed here.",
        "Include the original bundle and the time of the action. Do not repeat a router command just because its earlier record is missing."),
    "history_context_missing": (
        "No matching History was found in this period",
        "No readable History for this router's saved SIM and network identity was found in the review window. This does not establish that the wrong router or SIM was selected.",
        "The matching period may be absent or unavailable. The checker did not borrow learning from another connection to fill the gap.",
        "Check the report's window and keep this router's original folder or support ZIP. If the expected period is missing, send it with this report; do not reset History or copy another router's database."),
    "observation_identity_missing": (
        "Some measurements could not be assigned safely",
        "Some observations lack enough saved SIM and network information to link them to this connection.",
        "Those observations were excluded from this report rather than added to the wrong learning history.",
        "Keep the original data. Send the bundle if the excluded period covers the issue you are investigating."),
    "observations_missing": (
        "No usable measurement history was joined",
        "The checker could not link observations to the selected router's saved connection identities.",
        "It cannot describe the radio conditions throughout this period from the final snapshot alone.",
        "Check the selected folder or bundle. If it is correct, send it with this report; do not reset History."),
    "runs_missing": (
        "No saved run was found in this folder",
        "The selected folder does not contain a usable retained run for this review.",
        "There are no run records here to assess. This is not a report that the router is healthy or faulty.",
        "Choose the folder containing the 54GUC installation and its portable_runtime folder, or open its support ZIP."),
    "capture_incomplete": (
        "The support capture is incomplete",
        "The export records missing inputs or an unfinished capture.",
        "The readable parts are still checked, but the missing parts cannot be assessed.",
        "Keep this ZIP. If possible, create a new support bundle and include both when reporting the incident."),
    "record_missing": (
        "A required saved file is missing",
        "One of the records needed for this check was not included.",
        "The related check cannot be completed from the remaining files.",
        "Check the source filename below. Use the original app folder or a complete support ZIP rather than a selection of copied log files."),
    "record_unreadable": (
        "A saved file could not be read",
        "The record is unreadable, malformed, or beyond the checker's file safety limits.",
        "Other readable files are still checked; this file contributes no trusted result.",
        "Keep the original file unchanged. Check access permissions and send the original bundle with the error detail below."),
    "shutdown_unclean": (
        "Closing the app did not finish cleanly",
        "The shutdown receipt reports something other than complete.",
        "This is a shutdown problem to investigate. It does not automatically mean every worker failed during the run.",
        "Keep the logs from immediately before closing. Report whether the window closed normally and include the shutdown detail."),
    "modem_publication_error": (
        "A modem reading could not reach the app",
        "The reading-publication path saved an error.",
        "A router reply may have been received without its decoded reading reaching the app's current data.",
        "Note any blank or frozen readings at that time and send the bundle. The error alone is not a reason to change radio bands."),
    "learning_population_mismatch": (
        "Learning and forecasting use different saved results",
        "Their saved assessment populations do not agree.",
        "A forecast may not be based on the same accepted results shown by learning.",
        "Send the support ZIP for investigation. Keep History intact so the disagreement can be traced."),
    "history_loss": (
        "The app reported a History-saving problem",
        "History reports failed writes or lost records. The recorded counter and count are retained below.",
        "Some evidence or learning input may not have been saved. A write failure and a confirmed lost-record count are different details.",
        "Check free disk space and folder access. Preserve the existing History and send the bundle; do not delete or repair the database manually."),
    "history_owner_rejections": (
        "Some saved learning records were rejected",
        "The app's decision or assessment reader rejected records that did not meet its validation rules.",
        "Those records are not used as accepted learning evidence. The rejection count alone does not explain why they failed.",
        "Send the bundle if the rejected records cover a missing result. Keep their original files for diagnosis."),
    "screen_net_mismatch": (
        "The saved screen data disagrees with Internet status",
        "The screen record and Core's Internet record describe the same sample differently.",
        "The displayed connected or disconnected state may have been wrong for that sample.",
        "Note which page showed the unexpected state and send the bundle. This comparison uses saved screen data, not a screen recording."),
    "diagnostic_error": (
        "An error was recorded in the event journal",
        "The journal contains an entry marked error or critical. Its event name is retained below.",
        "The marker identifies an incident to inspect; it does not establish the cause or whether service was interrupted.",
        "Compare its time with Internet interruptions and command results. Send the bundle if you noticed a problem then."),
    "intelligence_fault": (
        "Brain has a saved internal-error entry",
        "The retained fault entry names the failed operation, error and available occurrence times.",
        "Its count may cover the whole retained lifetime rather than just this review. The detail states which times are established.",
        "Send the report and bundle with the affected time. Do not erase learning to remove the error message."),
    "fault_causes_evicted": (
        "Some older Brain error details are no longer retained",
        "The bounded fault history has removed older causes to stay within its retention limit.",
        "The remaining entries are useful but are not a complete list of past faults.",
        "Include an earlier saved support ZIP if you are investigating an older incident."),
    "fault_history_unreadable": (
        "Brain's error history could not be fully read",
        "The retained fault checkpoint could not be decoded completely.",
        "An empty list here cannot be treated as evidence that Brain had no faults.",
        "Keep the original bundle and send it with this report. Other readable checks remain available."),
    "evaluations_unreadable": (
        "Some Brain checks could not be read",
        "The saved evaluation history could not be decoded completely.",
        "Some decisions to act or hold may be missing from the hourly explanation.",
        "Keep the original bundle. Include the time whose decision you want explained when sending the report."),
    "history_family_limit": (
        "A History category exceeded the report limit",
        "Only a bounded number of records from this category could be included.",
        "Earlier records in that category may be absent, even though other categories were read.",
        "Use a support ZIP for the shorter period of interest and keep the original History unchanged."),
    "canonical_rejections": (
        "Some decision or assessment records are invalid",
        "The normal History validator rejected these records, so the checker excluded them.",
        "They cannot be used to establish a completed decision or quality result.",
        "Send the bundle if a result is missing. Do not edit rejected records to make them pass validation."),
    "observation_limit": (
        "The measurement history reached the report limit",
        "The maximum number of observations was returned for this review.",
        "Earlier measurements may be missing from the report.",
        "Keep the full History. Use a shorter support-export window to investigate a particular incident."),
    "decision_options_malformed": (
        "A decision's option list is unreadable",
        "The saved alternatives for this Brain decision are malformed.",
        "Other decisions are still readable, but these alternatives cannot be reconstructed from later forecasts.",
        "Send the bundle with the decision time if you need this choice investigated."),
    "run_limit": (
        "Too many saved runs for one review",
        "The checker reached its 128-run limit and did not open the remaining runs.",
        "This report does not cover every retained run in the selected folder.",
        "Keep the original folder. Review a support ZIP for the run of interest rather than deleting History to reduce the count."),
    "session_history_missing": (
        "This bundle has no usable session learning History",
        "The export lacks the qualified learning History needed for this connection.",
        "The checker can still inspect the saved app records, but not reconstruct the session's learning and decisions.",
        "Create a support ZIP that includes session History, if available, and retain this bundle too."),
    "command_outcomes_malformed": (
        "The saved action-result list is unreadable",
        "Command Authority's terminal-results list has an unexpected format.",
        "The checker cannot reliably say which requests completed from this list.",
        "Keep the bundle and send it with this report. Do not resend an action simply because its log is unreadable."),
    "command_outcome_fields_invalid": (
        "An action result has invalid fields or time",
        "Some saved command outcomes contain invalid timestamps or fields.",
        "Those entries cannot establish a trustworthy action timeline.",
        "Send the original bundle and the approximate time you requested the change."),
    "command_still_active": (
        "An action had no saved finish at the final snapshot",
        "The last saved Command Authority state still contains an active cellular command.",
        "It may have been captured while the request was in progress. The final snapshot alone does not establish whether it later finished.",
        "Check later terminal and shutdown records. If none exist, review the stopped app folder before deciding whether another request is needed."),
    "history_family_unreadable": (
        "One category of History could not be decoded",
        "The reader could not fully decode the named record family.",
        "Other categories remain readable, but this category cannot supply a complete result.",
        "Keep the original History and send the bundle with the named category shown below."),
    "observation_scope_unreadable": (
        "One connection's measurements could not be read",
        "An observation group for a saved connection context could not be decoded.",
        "The report may be missing measurements from that context.",
        "Send the original bundle. Do not merge another SIM's measurements to fill the gap."),
    "foreign_run_excluded": (
        "A different connection was kept separate",
        "A retained run belongs to another saved router, SIM or network context.",
        "It was excluded to avoid mixing learning and measurements from different connections.",
        "No repair is needed for this separation. Select that run's own bundle if you want to review it."),
    "run_time_invalid": (
        "A saved run has a future date",
        "A retained run contains an app-record time later than the time of this review.",
        "That run was excluded so it could not move the report into the future or select a different connection's records.",
        "Check the PC clock and the dates in the original files. Keep those files unchanged and include them if the saved date looks wrong."),
    "learning_identity_missing": (
        "The learning connection identity is missing",
        "No confirmed saved identity links this run to its own learning history.",
        "The checker did not guess a match from the router model or network name.",
        "Use this run's original bundle and folder. Send them if the connection identity should have been present."),
    "bundle_history_unreadable": (
        "The bundle's session History could not be fully read",
        "Session History is invalid, incomplete or exceeds one of the reader's safety limits.",
        "The detail below identifies the available reason. Other readable parts of the bundle are still checked.",
        "Keep this ZIP unchanged. If possible, create a new bundle covering a shorter period and include both."),
    "launcher_errors": (
        "The launcher log contains error markers",
        "The text log used to start 54GUC contains one or more error markers.",
        "Undated text can include earlier runs, so these markers are not automatically assigned to this session.",
        "Compare any recorded times with the run you are investigating. Send the original launcher log with the bundle."),
    "launcher_unreadable": (
        "The launcher log could not be fully read",
        "The startup text log is unreadable or exceeds its size limit.",
        "Some startup details may be absent from this review.",
        "Check file access and retain the original log. Include it if you are reporting a startup problem."),
    "evaluation_malformed": (
        "A saved Brain evaluation is malformed",
        "An individual evaluation did not have the required saved format.",
        "That entry cannot explain Brain's decision; the remaining valid entries are still used.",
        "Send the bundle if this covers the time you need explained. Keep the malformed record unchanged."),
    "proposal_finish_missing": (
        "A proposed change has no retained final result",
        "The review contains a Brain proposal but no matching terminal result or final command-quality proof.",
        "The proposal alone does not show that the router was changed or that a command is still running.",
        "Check the later saved results or review the app folder after normal shutdown. Do not replay the request from this missing record alone."),
    "adviser_options_malformed": (
        "An adviser's option list is unreadable",
        "The saved adviser snapshot has a malformed list of alternatives.",
        "The checker cannot reconstruct those option scores from later information.",
        "Send the original bundle with the snapshot time if this comparison is important."),
    "snapshot_unsupported": (
        "Part of a saved snapshot could not be checked",
        "A snapshot contains unsupported or invalid fields.",
        "The related check is incomplete; other readable checks continue.",
        "Use Log Checker 2.0 with the original bundle. If the message remains, send both to support."),
    "journal_limit": (
        "The event timeline reached the report limit",
        "The maximum number of diagnostic events was included.",
        "Earlier events may be missing even if the latest snapshot is available.",
        "Retain the original logs. Use a support ZIP for a shorter incident window when possible."),
    "journal_unreadable": (
        "The diagnostic timeline could not be read",
        "The saved event journal could not be opened or decoded through its normal reader.",
        "The final snapshots cannot replace the missing sequence of earlier events.",
        "Keep the database and bundle unchanged. Check file access and send them if the problem repeats."),
    "history_incomplete": (
        "The History review could not finish",
        "The selected History store could not be fully opened, copied read-only or decoded.",
        "Some decisions, observations or quality results may be absent. The original History was not changed.",
        "Check folder access and disk space. Preserve the database and send the support ZIP; do not reset History."),
    "command_receipt_invalid": (
        "An action's completion could not be verified",
        "The saved action receipt failed the normal command validator.",
        "Its displayed status can be listed, but it cannot establish that the matching Brain proposal completed.",
        "Send the original bundle with the action time. Do not repeat the command merely to replace a missing receipt."),
    "text_errors": (
        "A text log contains error markers",
        "The readable text contains error or traceback markers.",
        "A marker is a useful lead, not an established cause. It may describe an earlier incident or a handled failure.",
        "Compare its time and details with the app's events and any service interruption. Include the log when reporting the issue."),
    "text_unreadable": (
        "A text log could not be fully read",
        "The file could not be decoded or expanded within the reader's safety limits.",
        "No result is inferred from the unread portion; other readable logs are still checked.",
        "Keep the original file and bundle. Check access permissions and include the named file in a support report."),
    "pool_readback_stale": (
        "The shown LTE band list is older than the change",
        "The app still publishes an allowed-band pool read before a band write that may have taken effect.",
        "That old value cannot confirm the router's current configuration after the request.",
        "Send the bundle with the action time. Keep the requested and observed settings separate; do not resend a possibly applied write blindly."),
    "pool_snapshot_older_than_result": (
        "The device snapshot came before the action result",
        "The saved device snapshot is older than the completed action record being checked.",
        "These files cannot show the final band settings after that action.",
        "Use a support bundle saved after the action finishes. Do not repeat the band change just because of this item."),
    "command_copies_disagree": (
        "Two saved copies disagree about an action",
        "Copies with the same command identity and receipt sequence contain different outcomes.",
        "The checker cannot choose one copy as the trustworthy final result.",
        "Keep the original ZIP unchanged and send it with this report. Do not use either conflicting copy as a reason to repeat the action."),
}

FIELD_RESULTS = {
    "Unsupported": ("This profile does not provide a usable reading for this field.", "No action is needed for this field. Its absence is not a failed read."),
    "No native rule": ("This field has no direct router-reading rule in the saved profile; it may be supplied by another existing data owner.", "Use the field's recorded source when investigating it; do not count it as a successful native read."),
    "No saved conversion": ("The archive has no saved conversion for this field.", "Include a fresh support ZIP if you need this reading checked."),
    "Native read failed": ("A matching router read failed in the saved trace.", "Check the recorded time and whether later readings recovered. Include the bundle if the field remains unavailable."),
    "Context not retained": ("The conversion's required context was not saved.", "Keep this result as unavailable for replay; include a fresh bundle if needed."),
    "Native reply not retained": ("The exact router reply used for this conversion is outside the saved trace.", "Do not treat this as a failed conversion. Use a bundle containing that startup/read window if available."),
    "Private value withheld": ("A private value was removed from the shared evidence.", "No action is needed. Do not add credentials or private identifiers to make the report complete."),
    "Private input withheld": ("The conversion depends on an input removed for privacy.", "Keep the input private. This item cannot be replayed from the scrubbed bundle."),
    "Conversion failed": ("The saved router input could not be converted to the expected field value.", "Send the original bundle with this field name. Do not edit the router reply or profile to make it look valid."),
    "Replay matches": ("Repeating the saved conversion gives the same recorded value.", "No conversion discrepancy was found. Use the displayed units and recorded source when interpreting the measurement."),
    "Replay differs": ("Repeating the exact saved conversion gives a different value.", "Send the bundle and this field name for investigation; keep both values available."),
    "Unavailable": ("No usable value was present in the saved input.", "Check later readings if the field matters. A temporarily missing 5G reading is not numeric zero."),
    "Candidate changes value": ("The proposed profile converts this saved input differently.", "Review the old and proposed values and their units before adopting the profile."),
    "Native request not exercised": ("The saved trace did not exercise the request needed by the proposed profile.", "Retain this as untested; do not assume the proposed value is correct from another request."),
    "Missing reply": ("A required router reply is absent from the saved input.", "Include a support ZIP with the relevant reply if available."),
    "Decoded": ("The supplied router input was converted into a value.", "Check its recorded units and source when interpreting it. This result reports conversion, not a new router measurement."),
}


def field_explanation(status):
    meaning, action = FIELD_RESULTS.get(status, (
        "This field result is not recognised by Log Checker 2.0.",
        "Keep the recorded result below and send the bundle for review."))
    return dict(meaning=meaning, next_step=action)


def command_explanation(status):
    return {
        "settled": "The router accepted the request and the app observed the required settled configuration. This is not a service-quality grade.",
        "confirmed": "The requested configuration was confirmed by a later reading. A quality assessment is a separate result.",
        "accepted": "The router accepted the request; acceptance alone does not confirm the final configuration.",
        "unconfirmed": "The final configuration was not confirmed. The request may still have taken effect; check fresh readings before trying again.",
        "failed": "The request ended as failed. Read its recorded reason before deciding what to try next.",
        "cancelled": "The request was cancelled. The cancellation alone does not establish whether an earlier stage changed the router.",
        "rejected": "The request was refused. The recorded reason identifies which permission, guard or router response blocked it.",
        "timed_out": "The request's deadline expired without the required completion evidence. Check the current settings before repeating it.",
        "superseded": "A newer request replaced this request. Check the newer request's outcome.",
    }.get(status, "This command status is not recognised by Log Checker 2.0. Its recorded value is retained; no outcome is assumed.")


def recovery_explanation(phase):
    return {
        "armed": "Internet recovery is ready to respond; this state does not mean an outage is in progress.",
        "takeover": "Internet recovery has taken control of the current incident. Ordinary radio changes wait for its outcome.",
        "recovering": "The existing Internet recovery is working on the connection. 5G recovery waits while LTE service is unstable.",
        "verifying": "Recovery is checking independent Internet targets to establish whether an outage is present.",
        "device_recovery": "The router is already recovering. 54GUC is watching it instead of starting a competing recovery action.",
        "checking": "Internet recovery is checking the continued loss of service before taking stronger action.",
        "preparing": "The outage has continued. Internet recovery is preparing its existing rescue procedure.",
        "settling": "Recovery is waiting briefly for Internet service to return before taking further action.",
        "waiting_for_command": "Another cellular command is still active. Recovery is waiting rather than sending a conflicting command.",
        "pending_physical_dispatch": "A restart request exists, but exact evidence that it reached the modem has not yet been recorded.",
        "restart_rejected": "The restart request was rejected. This state does not report that a modem restart took place.",
        "rescue_window": "An Internet outage was confirmed. The existing recovery procedure may now restore the allowed LTE band pool; this state alone does not say the write happened.",
        "recovery_hold": "A modem restart was attempted. Recovery is observing the connection and will not repeat that restart during its hold period.",
        "recovery_state_invalid": "The saved recovery state could not be validated. Its recorded reason needs review before a recovery action can be attributed.",
        "cooldown": "Recovery is allowing the connection to settle before normal decisions resume.",
        "suspended": "Recovery is paused by a saved guard or changed configuration. The recorded reason is needed before any retry.",
        "complete": "This recovery reports completion. Later Internet status remains a separate observation.",
    }.get(phase, "A recovery state was saved. Its recorded value is shown below; this checker has no more specific explanation for that state.")
