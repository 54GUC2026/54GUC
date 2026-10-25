"""NGUI visual style and bundled font setup."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase


FONT_FAMILY = "Liberation Mono"
PAGE_TAB_FONT_PT = 10
BODY_FONT_PT = 10
BUTTON_FONT_PT = 9
FOOTER_FONT_PT = 12
USER_BUTTON_IDLE_BACKGROUND = "#00a86b"
USER_BUTTON_IDLE_HOVER_BACKGROUND = "#00b877"
USER_BUTTON_IDLE_TEXT = "#000000"
USER_BUTTON_ACTIVE_BACKGROUND = "#080d12"
USER_BUTTON_ACTIVE_TEXT = "#ffffff"
_FONT_FILES = (
    "LiberationMono-Regular.ttf",
    "LiberationMono-Bold.ttf",
    "LiberationMono-Italic.ttf",
    "LiberationMono-BoldItalic.ttf",
)
_FONTS_INSTALLED = False

MEASUREMENT_ACTION_STYLE = (
    "QPushButton { background-color: #00a86b; color: #000000; "
    "border: 1px solid #3a5263; border-radius: 4px; }"
    "QPushButton:hover { background-color: #00b877; border-color: #00d37f; }"
    "QPushButton:checked { background-color: #080d12; color: #ffffff; border-color: #ffffff; }"
    "QPushButton:disabled { background-color: #101720; color: #6f8092; border-color: #243752; }"
)


def install_bundled_fonts() -> str:
    """Install the bundled NGUI font or fail before rendering."""
    global _FONTS_INSTALLED
    if _FONTS_INSTALLED:
        return FONT_FAMILY
    font_dir = Path(__file__).resolve().parents[1] / "fonts"
    failures = [name for name in _FONT_FILES if QFontDatabase.addApplicationFont(str(font_dir / name)) < 0]
    if failures:
        raise RuntimeError(f"NGUI bundled font load failed: {', '.join(failures)}")
    _FONTS_INSTALLED = True
    return FONT_FAMILY


def user_button_stylesheet(
    *,
    font_pt: int,
    min_height: int = 32,
    padding: str = "4px 10px",
    border_radius: int = 6,
) -> str:
    """Return the green-idle and dark-active button style."""
    return (
        f"QPushButton {{ background-color: {USER_BUTTON_IDLE_BACKGROUND}; "
        f"color: {USER_BUTTON_IDLE_TEXT}; border: 1px solid #00d37f; "
        f"border-radius: {border_radius}px; font-size: {font_pt}pt; "
        f"font-weight: bold; min-height: {min_height}px; padding: {padding}; }}"
        f"QPushButton:hover {{ background-color: {USER_BUTTON_IDLE_HOVER_BACKGROUND}; "
        f"color: {USER_BUTTON_IDLE_TEXT}; }}"
        f"QPushButton:pressed, QPushButton:checked {{ "
        f"background-color: {USER_BUTTON_ACTIVE_BACKGROUND}; "
        f"color: {USER_BUTTON_ACTIVE_TEXT}; "
        f"border-color: {USER_BUTTON_ACTIVE_TEXT}; }}"
    )

_STYLESHEET_TEMPLATE = """
QWidget {
    background: #080d12;
    color: #d9e2ec;
    selection-background-color: #17333d;
    selection-color: #ffffff;
    font-family: "Liberation Mono";
    font-size: {body_font_pt}pt;
}
QGroupBox {
    border: 1px solid #203044;
    margin-top: 8px;
    padding-top: 5px;
    color: #77a0c5;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 6px;
    padding: 0 3px;
}
QPushButton {
    background: #00a86b;
    border: 1px solid #00d37f;
    border-radius: 3px;
    min-height: 23px;
    padding: 1px 8px;
    color: #000000;
    font-size: {button_font_pt}pt;
}
QPushButton:hover { background: #00b877; color: #000000; }
QPushButton:disabled {
    background: #101720;
    border: 1px solid #243752;
    color: #6f8092;
}
QPushButton:pressed, QPushButton:checked {
    background: #080d12;
    border-color: #ffffff;
    color: #ffffff;
}
QPushButton[nguiRole="page_nav"][pageSelected="false"] {
    background: #2f80ed;
    border: 1px solid #2f80ed;
    color: #ffffff;
    font-size: 10pt;
    font-weight: bold;
    min-height: 30px;
    padding: 4px 14px;
}
QPushButton[nguiRole="page_nav"][pageSelected="true"] {
    background: #080d12;
    border: 1px solid #ffffff;
    color: #ffffff;
    font-size: 10pt;
    font-weight: bold;
    min-height: 30px;
    padding: 4px 14px;
}
QPushButton[nguiRole="utility_primary"] {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
    font-weight: bold;
    font-size: 10pt;
}
QPushButton[nguiRole="utility_outline"] {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
    font-weight: bold;
    font-size: 10pt;
}
QPushButton[nguiRole="destructive"] {
    background: #b71c1c;
    border: 1px solid #ff5252;
    color: #ffffff;
    font-weight: bold;
    font-size: 10pt;
}
QPushButton[nguiRole="utility_primary"]:checked,
QPushButton[nguiRole="utility_primary"]:pressed,
QPushButton[nguiRole="utility_outline"]:checked,
QPushButton[nguiRole="utility_outline"]:pressed,
QPushButton[nguiRole="destructive"]:checked,
QPushButton[nguiRole="destructive"]:pressed {
    background: #080d12;
    border-color: #ffffff;
    color: #ffffff;
}
QPushButton[nguiRole="wifi_toggle"] {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
    font-size: 10pt;
    font-weight: bold;
}
QPushButton[nguiRole="wifi_toggle"]:checked,
QPushButton[nguiRole="wifi_toggle"]:pressed,
QPushButton[nguiRole="wifi_toggle"]:checked:disabled {
    background: #080d12;
    border: 1px solid #ffffff;
    color: #ffffff;
}
QPushButton[nguiRole="wifi_toggle"]:disabled {
    background: #101720;
    border: 1px solid #243752;
    color: #ffffff;
}
QPushButton[nguiRole="wifi_toggle"][nguiWifiInteractionState="interlocked"]:disabled {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
}
QPushButton[nguiRole="state_choice"] {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
    font-weight: bold;
}
QPushButton[nguiRole="state_choice"]:hover {
    background: #00b877;
    border-color: #37ef9d;
    color: #000000;
}
QPushButton[nguiRole="state_choice"]:checked,
QPushButton[nguiRole="state_choice"]:checked:hover,
QPushButton[nguiRole="state_choice"]:checked:disabled {
    background: #080d12;
    border: 1px solid #ffffff;
    color: #ffffff;
}
QPushButton[nguiRole="state_choice"]:disabled {
    background: #315f50;
    border: 1px solid #6b8d82;
    color: #d9e2ec;
}
QPushButton#deviceConnectButton {
    background: #00a86b;
    border: 1px solid #00d37f;
    color: #000000;
    font-weight: bold;
}
QPushButton#deviceConnectButton:hover {
    background: #00e68a;
    border-color: #00e68a;
}
QPushButton#deviceConnectButton:checked,
QPushButton#deviceConnectButton:pressed,
QPushButton#deviceConnectButton:checked:hover {
    background: #080d12;
    border-color: #ffffff;
    color: #ffffff;
}
QLabel#deviceLicenseState {
    color: #00d37f;
    font-weight: bold;
    font-size: 10pt;
}
QLabel[nguiRole="utility_status"] {
    color: #d9e2ec;
    font-size: 10pt;
}
QLabel#truthFieldName {
    color: #77a0c5;
}
QLabel[nguiRole="truthFieldValue"] {
    color: #ffffff;
    font-weight: bold;
}
QLabel[nguiRole="truthFieldValue"][nguiValueState="good"] {
    color: #00d37f;
}
QLabel[nguiRole="truthFieldValue"][nguiValueState="bad"] {
    color: #ff4055;
}
QLabel[nguiRole="truthFieldValue"][nguiValueState="neutral"] {
    color: #ffffff;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #0d141c;
    border: 1px solid #29405a;
    border-radius: 3px;
    min-height: 23px;
    padding: 1px 5px;
    color: #d9e2ec;
    placeholder-text-color: #8fa4b8;
}
QPlainTextEdit, QTextEdit {
    background: #0d141c;
    color: #d9e2ec;
    placeholder-text-color: #8fa4b8;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {
    color: #8fa4b8;
}
/* Native sections and popup views do not share the table body's painting. */
QAbstractItemView {
    background-color: #0d141c;
    alternate-background-color: #101c22;
    color: #d9e2ec;
    selection-background-color: #17333d;
    selection-color: #ffffff;
}
QAbstractItemView::item:selected {
    background-color: #17333d;
    color: #ffffff;
}
QAbstractItemView::item:disabled {
    color: #8fa4b8;
}
QHeaderView::section, QTableCornerButton::section {
    background-color: #112031;
    color: #edf5f7;
    border: 1px solid #3a5263;
}
QMenu, QToolTip {
    background-color: #112031;
    color: #edf5f7;
    border: 1px solid #3a5263;
}
QMenu::item:selected {
    background-color: #17333d;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #8fa4b8;
}
QLineEdit[nguiRole="device_entry_input"], QComboBox[nguiRole="device_entry_input"] {
    border: 1px dotted #d9e2ec;
    border-radius: 6px;
    color: #ffffff;
}
QScrollArea { border: 0; }
QStatusBar {
    background: #080d12;
    border-top: 1px solid #203044;
    min-height: 24px;
}
QStatusBar::item { border: 0; }
QLabel#operationalFooterSeparator { color: #77a0c5; }
QLabel#operationalFooterValue { color: #ffffff; }
QScrollBar:vertical { background: #0d141c; width: 10px; }
QScrollBar::handle:vertical { background: #29405a; min-height: 30px; }
QScrollBar:horizontal { background: #0d141c; height: 10px; }
QScrollBar::handle:horizontal { background: #29405a; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line {
    background: #112031;
    border: 1px solid #29405a;
}
QLabel#radioHeaderBadge {
    color: #00d37f;
    background: #112031;
    border: 2px solid #00d37f;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 10pt;
    font-weight: bold;
    min-height: 36px;
    min-width: 80px;
}
QLabel#radioHeaderChip {
    color: #00d37f;
    background: #112031;
    border: 1px solid #00d37f;
    border-radius: 4px;
    padding: 5px 12px;
    font-size: 10pt;
    font-weight: bold;
}
QLabel#radioHeaderClock { color: #77a0c5; font-size: 10pt; }
QLabel[lit="false"] {
    background: #112031;
    border: 1px solid #203044;
    border-radius: 7px;
}
QLabel[lit="true"] {
    background: #ffffff;
    border: 1px solid #ffffff;
    border-radius: 7px;
}
QWidget#deviceSignInCard {
    border: 1px solid #00d37f;
    border-radius: 6px;
}
QWidget#deviceCapabilityPanel {
    border: 1px solid #203044;
    border-radius: 6px;
}
QWidget#deviceCapabilityPanel QLabel#truthFieldName,
QWidget#deviceCapabilityPanel QLabel[nguiRole="truthFieldValue"] {
    font-size: 9pt;
}
QLabel#deviceSectionHeading {
    color: #2f80ed;
    font-weight: bold;
}
QLabel#deviceImportantTitle {
    color: #ffffff;
    font-weight: bold;
}
QLabel#deviceImportantBody {
    color: #77a0c5;
}
QLabel#nguiDeviceNetworkDiagnostic, QLabel#nguiDeviceNetworkFeedback {
    font-size: 12pt;
}
/* The two measurement pages share hierarchy; other pages retain their style. */
QWidget#nguiDashboardPage QGroupBox, QWidget#nguiDeveloperPage QGroupBox {
    border: 1px solid #293c49;
    border-radius: 5px;
    color: #9aafbe;
    margin-top: 10px;
    padding-top: 8px;
}
QWidget#nguiDashboardPage QLabel[nguiRole="page_heading"],
QWidget#nguiDeveloperPage QLabel[nguiRole="page_heading"] {
    color: #e3edf4;
    font-size: 18pt;
    font-weight: bold;
}
QWidget#nguiDashboardPage QLabel[nguiRole="overview_value"] {
    color: #e3edf4;
    font-size: 20pt;
    font-weight: bold;
}
QWidget#nguiDashboardPage QLabel[nguiRole="section_heading"],
QWidget#nguiDeveloperPage QLabel[nguiRole="section_heading"] {
    color: #9aafbe;
    font-weight: bold;
}
QWidget#nguiDashboardPage QPushButton,
QWidget#nguiDeveloperPage QPushButton {
    background: #00a86b;
    color: #000000;
    border: 1px solid #00d37f;
    border-radius: 4px;
    min-height: 25px;
}
QWidget#nguiDashboardPage QPushButton:hover,
QWidget#nguiDeveloperPage QPushButton:hover {
    background: #00b877;
    border-color: #00d37f;
}
QWidget#nguiDashboardPage QPushButton:checked,
QWidget#nguiDeveloperPage QPushButton:checked {
    background: #080d12;
    border-color: #ffffff;
    color: #ffffff;
}
QWidget#nguiDashboardPage QPushButton:disabled,
QWidget#nguiDeveloperPage QPushButton:disabled {
    background: #101720;
    border-color: #243752;
    color: #6f8092;
}
QWidget#nguiDashboardPage QPushButton:checked:disabled,
QWidget#nguiDeveloperPage QPushButton:checked:disabled {
    background: #080d12;
    color: #ffffff;
    border-color: #ffffff;
}
"""

STYLESHEET = (
    _STYLESHEET_TEMPLATE.replace("{body_font_pt}", str(BODY_FONT_PT))
    .replace("{button_font_pt}", str(BUTTON_FONT_PT))
)


__all__ = [
    "BODY_FONT_PT",
    "BUTTON_FONT_PT",
    "FONT_FAMILY",
    "FOOTER_FONT_PT",
    "PAGE_TAB_FONT_PT",
    "STYLESHEET",
    "USER_BUTTON_ACTIVE_BACKGROUND",
    "USER_BUTTON_ACTIVE_TEXT",
    "USER_BUTTON_IDLE_BACKGROUND",
    "USER_BUTTON_IDLE_HOVER_BACKGROUND",
    "USER_BUTTON_IDLE_TEXT",
    "install_bundled_fonts",
    "user_button_stylesheet",
]
