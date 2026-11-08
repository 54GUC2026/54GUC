"""
Single operator-facing projection builder from canonical action and incident truth.
"""

from __future__ import annotations

from typing import Any, Dict


def build_operator_projection(
    *,
    action_outcome: Dict[str, Any] | None,
    incident_state: Dict[str, Any] | None,
) -> Dict[str, Any]:
    action_outcome = dict(action_outcome or {})
    incident_state = dict(incident_state or {})
    incident_class = str(incident_state.get("incident_class", "healthy") or "healthy")
    action_scope = str(action_outcome.get("action_scope", "no_action") or "no_action")
    action_semantic = str(action_outcome.get("semantic", "unknown") or "unknown")
    projection_class = "standard"
    status_text = ""
    if incident_class == "post_action_instability":
        if action_scope == "action" and action_semantic == "beneficial":
            projection_class = "successful_action_settling"
            status_text = "Recent action succeeded. Link is settling before the next decision."
        elif action_scope == "action" and action_semantic == "harmful":
            projection_class = "harmful_action_instability"
            status_text = "Recent action hurt the link. Brain is backing off while the modem settles."
        else:
            projection_class = "recent_change_settling"
            status_text = "Recent change is still settling. Brain is holding and waiting."
    elif action_scope == "action" and action_semantic == "beneficial":
        projection_class = "successful_action_observe"
        status_text = "Recent action improved conditions. Brain is observing the new state."
    elif action_scope == "action" and action_semantic == "harmful":
        projection_class = "harmful_action_observe"
        status_text = "Recent action worsened conditions. Brain is reassessing before the next change."
    return {
        "projection_class": projection_class,
        "status_text": status_text,
        "action_scope": action_scope,
        "action_semantic": action_semantic,
        "incident_class": incident_class,
    }
