"""Two-stage confirmation for dangerous NGUI manual actions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from .style import PAGE_TAB_FONT_PT, BUTTON_FONT_PT

from ..interactions import ControlFamily, IntentKind, NGUIInteractionRegister
from .interactions import bind_local_button, bind_timer, publish_intent


APPLY_BUTTON_STYLE = (
    "QPushButton { background-color: #00d37f; color: #000000; "
    f"border: 1px solid #00d37f; border-radius: 6px; font-size: {PAGE_TAB_FONT_PT}pt; "
    "font-weight: bold; min-height: 32px; padding: 4px 10px; }"
    "QPushButton:hover { background-color: #00bfa5; color: #000000; }"
    "QPushButton:pressed { background-color: #080d12; color: #ffffff; "
    "border-color: #080d12; }"
    "QPushButton:disabled { background-color: #111b27; color: #52708d; "
    "border: 1px solid #20344a; }"
)

DESTRUCTIVE_BUTTON_STYLE = APPLY_BUTTON_STYLE + (
    "QPushButton { background-color: #b32626; color: #ffffff; border-color: #ff6666; }"
    "QPushButton:hover { background-color: #cc0000; color: #ffffff; }"
)

CONFIRM_BUTTON_STYLE = (
    "QPushButton { background-color: #cc0000; color: #ffffff; "
    f"border: 1px solid #ff6666; border-radius: 6px; font-size: {PAGE_TAB_FONT_PT}pt; "
    "font-weight: 800; min-height: 32px; padding: 4px 10px; }"
    "QPushButton:hover { background-color: #e00000; color: #ffffff; }"
    "QPushButton:pressed { background-color: #a80000; color: #ffffff; }"
)


class NGUIConfirmationCoordinator:
    """Own the dangerous request currently armed by the renderer."""

    def __init__(self) -> None:
        self._armed_button: NGUITwoStageConfirmButton | None = None
        self._armed_request: Any | None = None

    def arm(
        self,
        button: "NGUITwoStageConfirmButton",
        request: Any,
    ) -> None:
        if self._armed_button is not None and self._armed_button is not button:
            self._armed_button._disarm_local()
        self._armed_button = button
        self._armed_request = request
        button._arm_local()

    def confirms(
        self,
        button: "NGUITwoStageConfirmButton",
        request: Any,
    ) -> bool:
        return self._armed_button is button and self._armed_request == request

    def disarm(self, button: "NGUITwoStageConfirmButton" | None = None) -> None:
        if button is not None and self._armed_button is not button:
            button._disarm_local()
            return
        armed = self._armed_button
        self._armed_button = None
        self._armed_request = None
        if armed is not None:
            armed._disarm_local()

    @property
    def armed_button(self) -> "NGUITwoStageConfirmButton" | None:
        return self._armed_button


class NGUITwoStageConfirmButton(QPushButton):
    """Arm on the first click and send the unchanged request on the second."""

    def __init__(
        self,
        request: Callable[[], Any | None],
        register: NGUIInteractionRegister,
        kind: IntentKind,
        *,
        payload_from_request: Callable[[Any], Mapping[str, Any]] | None = None,
        label: str = "APPLY",
        coordinator: NGUIConfirmationCoordinator | None = None,
        timeout_ms: int = 5_000,
        destructive: bool = False,
        compact: bool = False,
        idle_style: str = APPLY_BUTTON_STYLE,
    ) -> None:
        self._base_label = str(label or "").strip() or "APPLY"
        super().__init__(self._base_label)
        self._request = request
        self._register = register
        self._intent_kind = kind
        self._payload_from_request = payload_from_request
        self._coordinator = coordinator or NGUIConfirmationCoordinator()
        self._destructive = bool(destructive)
        self._idle_style = DESTRUCTIVE_BUTTON_STYLE if self._destructive else idle_style
        self._size_style = (
            f"QPushButton {{ font-size: {BUTTON_FONT_PT}pt; min-height: 0px; "
            "padding: 1px 8px; border-radius: 3px; }" if compact else ""
        )
        self.confirm_timer = QTimer(self)
        self.confirm_timer.setSingleShot(True)
        self.confirm_timer.setInterval(int(timeout_ms))
        bind_timer(self.confirm_timer, self.disarm)
        bind_local_button(
            self,
            self._arm_or_confirm,
            intent_kind=kind,
            family=ControlFamily.TWO_STAGE_CONFIRM,
        )
        self.setProperty(
            "nguiRole",
            "destructive" if self._destructive else "user_action",
        )
        self.setStyleSheet(self._idle_style + self._size_style)

    @property
    def is_armed(self) -> bool:
        return self._coordinator.armed_button is self

    def set_compact_size(self, width: int, height: int) -> None:
        # GUI toolkit reapplies stylesheet minimums when confirmation changes color.
        # Keep the reference height in the style as well as the widget limits.
        content_height = max(0, height - 4)  # two 1px borders and 1px padding
        self._size_style = (
            f"QPushButton {{ font-size: {BUTTON_FONT_PT}pt; "
            f"min-height: {content_height}px; max-height: {content_height}px; "
            "padding: 1px 8px; border-radius: 3px; }"
        )
        base = CONFIRM_BUTTON_STYLE if self.is_armed else self._idle_style
        self.setStyleSheet(base + self._size_style)
        self.setFixedSize(width, height)

    @property
    def base_label(self) -> str:
        return self._base_label

    def set_base_label(self, label: str) -> None:
        normalized = str(label or "").strip()
        if not normalized:
            raise ValueError("confirmation button base label cannot be empty")
        if normalized == self._base_label:
            return
        self._base_label = normalized
        if self.is_armed:
            self.disarm()
        else:
            self.setText(self._base_label)

    def disarm(self) -> None:
        self._coordinator.disarm(self)

    def _disarm_local(self) -> None:
        self.confirm_timer.stop()
        self.setText(self._base_label)
        self.setStyleSheet(self._idle_style + self._size_style)

    def _arm_local(self) -> None:
        self.setText("CONFIRM?")
        self.setStyleSheet(CONFIRM_BUTTON_STYLE + self._size_style)
        self.confirm_timer.start()

    def _arm_or_confirm(self) -> None:
        request = self._request()
        if request is None:
            self.disarm()
            return
        if self._coordinator.confirms(self, request):
            self.disarm()
            if self._payload_from_request is not None:
                payload = dict(self._payload_from_request(request))
            elif isinstance(request, Mapping):
                payload = dict(request)
            else:
                payload = {}
            publish_intent(
                self._register,
                self._intent_kind,
                payload=payload,
                family=ControlFamily.TWO_STAGE_CONFIRM,
            )
            return
        self._coordinator.arm(self, request)


__all__ = (
    "APPLY_BUTTON_STYLE",
    "CONFIRM_BUTTON_STYLE",
    "NGUIConfirmationCoordinator",
    "NGUITwoStageConfirmButton",
)
