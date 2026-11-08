"""Connect GUI toolkit signals to typed user intents."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer

from ..control_contracts import (
    CGF_GUI_CONTROL_MANIFEST,
    CONTROL_CONTRACT_VERSION,
    GUIControlKind,
)
from ..interactions import (
    INTENT_REGISTRY,
    ControlFamily,
    IntentKind,
    NGUIInteractionRegister,
    UserIntent,
)


def publish_intent(
    register: NGUIInteractionRegister,
    kind: IntentKind,
    *,
    payload: dict[str, Any] | None = None,
    created_at: float | None = None,
    family: ControlFamily | None = None,
) -> UserIntent:
    """Send renderer intents to the interaction register."""

    _require_control_family(kind, family)
    return register.publish(
        kind,
        created_at=float(created_at if created_at is not None else time.monotonic()),
        payload=payload or {},
    )


def bind_button(
    button: Any,
    register: NGUIInteractionRegister,
    kind: IntentKind,
    payload: Callable[[], dict[str, Any]] | None = None,
    *,
    local_action: Callable[[], None] | None = None,
    family: ControlFamily | None = None,
) -> None:
    resolved_family = _require_control_family(kind, family)
    _stamp_control(button, kind, resolved_family)

    def publish(_checked: bool = False) -> None:
        publish_intent(
            register,
            kind,
            payload=payload() if payload is not None else {},
            family=resolved_family,
        )
        if local_action is not None:
            local_action()

    button.clicked.connect(publish)


def bind_toggle(
    widget: Any,
    register: NGUIInteractionRegister,
    kind: IntentKind,
    payload: Callable[[], dict[str, Any]],
) -> None:
    """Publish a checkbox or toggle change through the NMI lane."""

    family = _require_control_family(kind, ControlFamily.READBACK_TOGGLE)
    _stamp_control(widget, kind, family)

    def publish(_state: int = 0) -> None:
        publish_intent(
            register,
            kind,
            payload=payload(),
            family=family,
        )

    widget.stateChanged.connect(publish)


def bind_local_button(
    button: Any,
    callback: Callable[[], None],
    *,
    intent_kind: IntentKind | None = None,
    family: ControlFamily | None = None,
) -> None:
    """Connect a renderer-local button through the NMI lane."""

    if intent_kind is not None:
        resolved_family = _require_control_family(intent_kind, family)
        _stamp_control(button, intent_kind, resolved_family)
    elif family is not None:
        raise ValueError("local control family requires an intent contract")

    def run(_checked: bool = False) -> None:
        callback()

    button.clicked.connect(run)


def bind_text_draft(widget: Any, callback: Callable[[str], None]) -> None:
    """Publish unblocked QLineEdit changes to the draft owner."""

    def run(text: str) -> None:
        callback(text)

    widget.textChanged.connect(run)


def bind_numeric_edited(widget: Any, callback: Callable[[float], None]) -> None:
    """Publish numeric changes to the GUI-independent draft owner."""

    def run_value(value: float) -> None:
        callback(float(value))

    def run_finished() -> None:
        callback(float(widget.value()))

    widget.valueChanged.connect(run_value)
    widget.editingFinished.connect(run_finished)


def bind_return_intent(
    widget: Any,
    register: NGUIInteractionRegister,
    kind: IntentKind,
    payload: Callable[[], dict[str, Any]],
) -> None:
    """Publish the typed action when an editor receives Return or Enter."""

    family = _require_control_family(kind, ControlFamily.VALUE_SUBMIT)
    _stamp_control(widget, kind, family)

    def publish() -> None:
        publish_intent(
            register,
            kind,
            payload=payload(),
            family=family,
        )

    editor = widget.lineEdit() if callable(getattr(widget, "lineEdit", None)) else widget
    editor.returnPressed.connect(publish)


def bind_combo_changed(widget: Any, callback: Callable[[], None]) -> None:
    """Connect a renderer-local combo change through the NMI lane."""

    def run(_index: int = 0) -> None:
        callback()

    widget.currentIndexChanged.connect(run)


def bind_tab_selected(widget: Any, callback: Callable[[int], None]) -> None:
    """Coalesce mouse/keyboard selection and a click on the current tab."""
    pending = False

    def dispatch() -> None:
        nonlocal pending
        pending = False
        callback(widget.currentIndex())

    def selected(_index: int) -> None:
        nonlocal pending
        if not pending:
            pending = True
            QTimer.singleShot(0, widget, dispatch)

    widget.tabBarClicked.connect(selected)
    widget.currentChanged.connect(selected)


def stamp_draft_control(widget: Any) -> None:
    """Bind an editor or selector to its draft and submit contract."""

    object_name = str(widget.objectName() or "").strip()
    contract = CGF_GUI_CONTROL_MANIFEST.get(object_name)
    if contract is None or contract.kind is GUIControlKind.BUTTON:
        raise ValueError(f"undeclared NGUI draft control: {object_name or '<unnamed>'}")
    if contract.draft_owner is None:
        raise ValueError(f"NGUI draft owner missing: {object_name}")
    widget.setProperty("nguiControlContract", CONTROL_CONTRACT_VERSION)
    widget.setProperty("nguiControlKind", contract.kind.value)
    widget.setProperty("nguiDraftOwner", contract.draft_owner.value)
    widget.setProperty(
        "nguiSubmitIntents",
        "|".join(intent.value for intent in contract.submit_intents),
    )
    widget.setProperty(
        "nguiDestinationOwners",
        "|".join(contract.destinations),
    )


def bind_dialog_buttons(
    buttons: Any, accept: Callable[[], None], reject: Callable[[], None]
) -> None:
    """Connect modal dialog OK and Cancel through the renderer lane."""

    buttons.accepted.connect(accept)
    buttons.rejected.connect(reject)


def bind_timer(timer: QTimer, callback: Callable[[], None]) -> None:
    timer.timeout.connect(callback)


def _require_control_family(
    kind: IntentKind,
    family: ControlFamily | None,
) -> ControlFamily:
    spec = INTENT_REGISTRY[kind]
    resolved = family
    if resolved is None:
        if len(spec.control_families) == 1:
            resolved = next(iter(spec.control_families))
        elif ControlFamily.MOMENTARY in spec.control_families:
            resolved = ControlFamily.MOMENTARY
        else:
            raise ValueError(f"{kind.value}: control family must be explicit")
    if resolved not in spec.control_families:
        raise ValueError(
            f"{kind.value}: illegal {resolved.value} control; "
            f"allowed={sorted(item.value for item in spec.control_families)}"
        )
    return resolved


def _stamp_control(widget: Any, kind: IntentKind, family: ControlFamily) -> None:
    spec = INTENT_REGISTRY[kind]
    widget.setProperty("nguiControlContract", CONTROL_CONTRACT_VERSION)
    widget.setProperty("nguiControlFamily", family.value)
    widget.setProperty("nguiIntentKind", kind.value)
    widget.setProperty("nguiDestinationOwner", spec.destination.value)


__all__ = (
    "CONTROL_CONTRACT_VERSION",
    "bind_button",
    "bind_combo_changed",
    "bind_dialog_buttons",
    "bind_local_button",
    "bind_numeric_edited",
    "bind_return_intent",
    "bind_text_draft",
    "bind_timer",
    "bind_toggle",
    "publish_intent",
    "stamp_draft_control",
)
