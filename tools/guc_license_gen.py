#!/usr/bin/env python3
"""54GUC License Generator — standalone Windows tool.

Run the source tool with the configured native interpreter.
Use scripts/build_guc_license_gen_windows.ps1 for the verified executable.

Generates license keys for 54GUC modem binding.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QPlainTextEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from arch4.licensing import (
    _derive_key as _arch4_derive_key,
    decode_license_nonce,
    generate_license as _arch4_generate_license,
    generate_token as _arch4_generate_token,
    license_tier_from_nonce,
    normalize_license_tier,
)


def generate_token(modem_serial: str) -> str:
    """16-char uppercase hex token from modem serial."""
    return _arch4_generate_token(modem_serial)


def validate_serial_input(serial: str) -> str | None:
    """Return an error message if *serial* is unsuitable, else None.

    Checks:
    - Not empty / whitespace-only
    - Not unreasonably short (< 4 chars after strip)
    - Not unreasonably long (> 64 chars after strip)
    - No control characters (U+0000–U+001F except space)
    - No non-BMP characters (emoji, surrogates — paste artifacts)
    - No leading/trailing whitespace that would silently change the key
    - Not a placeholder or obviously invalid value
    """
    if serial is None:
        return "Enter a modem serial or IMEI."
    s = serial.strip()
    if not s:
        return "Enter a modem serial or IMEI."
    if len(s) < 4:
        return f"Serial too short ({len(s)} chars) — expected IMEI or device serial."
    if len(s) > 64:
        return f"Serial too long ({len(s)} chars) — check for extra whitespace."
    if any(ord(c) < 0x20 for c in s):
        return (
            "Serial contains control characters — paste may have added invisible bytes."
        )
    if any(ord(c) > 0xFFFF for c in s):
        return "Serial contains emoji or special symbols — use only device characters."
    if serial != s:
        return (
            "Serial has leading/trailing whitespace — it will be stripped before use."
        )
    # Reject placeholder patterns.
    if s == "0" * len(s):
        return "Serial is all zeros — use the actual modem serial or IMEI."
    if len(s) >= 4 and len(set(s)) == 1:
        return f"Serial is all '{s[0]}' — use the actual modem serial or IMEI."
    if s.lower() in {"test", "none", "null", "n/a", "unknown", "serial", "imei"}:
        return f"'{s}' looks like a placeholder — use the actual modem serial or IMEI."
    return None


def format_license_block(
    serial: str, token: str, nonce: str, key: str, tier: str = ""
) -> str:
    """Build the clipboard text block for email/paste.

    Centralised so both GUI and any future CLI tool produce identical output.
    Fields are left-aligned with fixed-width labels for clean paste.
    """
    tier_value = normalize_license_tier(tier) or license_tier_from_nonce(nonce)
    lines = [
        "54GUC License Details",
        "---------------------",
        f"Serial: {serial}",
        f"Token:  {token}",
    ]
    if tier_value:
        lines.append(f"Tier:   {tier_value}")
    lines.extend(
        [
            f"Nonce:  {nonce}",
            f"Key:    {key}",
            "",
            "Enter the Nonce and Key in 54GUC Settings to activate.",
        ]
    )
    return "\n".join(lines)


def validate_license_output(token: str, nonce: str, key: str) -> str | None:
    """Verify generated license fields are well-formed.

    Returns error message if any field is malformed, else None.
    Used as a post-generation sanity check.
    """
    if not token or len(token) != 16:
        return f"Token length {len(token or '')} — expected 16."
    if not all(c in "0123456789ABCDEF" for c in token):
        return "Token contains non-hex characters."
    nonce_tier, nonce_body = decode_license_nonce(nonce)
    if not nonce_body:
        return (
            "Nonce format invalid — expected 16 hex chars or tier-code + 16 hex chars."
        )
    if nonce_tier and len(nonce) != 18:
        return f"Tier-bound nonce length {len(nonce or '')} — expected 18."
    if not nonce_tier and len(nonce) != 16:
        return f"Legacy nonce length {len(nonce or '')} — expected 16."
    if not key or len(key) != 20:
        return f"Key length {len(key or '')} — expected 20."
    if not all(c in "0123456789ABCDEF" for c in key):
        return "Key contains non-hex characters."
    return None


def validate_export_payload(block: str) -> str | None:
    """Verify a formatted license block is safe for clipboard/email.

    Returns error message if block is malformed, else None.
    Checks:
    - Non-empty
    - No NUL bytes
    - Contains all four field labels
    - Uses only \\n line endings (no bare \\r)
    """
    if not block or not block.strip():
        return "Export payload is empty."
    if "\x00" in block:
        return "Export payload contains NUL bytes."
    if "\r" in block:
        return "Export payload contains carriage returns — expected Unix line endings."
    for label in ("Serial:", "Token:", "Nonce:", "Key:"):
        if label not in block:
            return f"Export payload missing {label} field."
    return None


def generate_license(modem_serial: str, tier: str = "") -> tuple[str, str]:
    """Generate (nonce, key) pair for a modem serial."""
    return _arch4_generate_license(modem_serial, tier=tier)


def _derive_key(serial: str, nonce: str) -> str:
    """Expose the canonical deterministic derivation to generator tests."""
    return _arch4_derive_key(serial, nonce)


GENERATOR_VERSION = "2.0"


class LicenseGenWindow(QMainWindow):
    """Two visible fields; canonical generation and paste format stay unchanged."""
    def __init__(self, *, tier="internal_full"):
        super().__init__()
        self.tier = normalize_license_tier(tier)
        if not self.tier:
            raise ValueError("Choose an existing valid licence tier")
        self.setWindowTitle("54GUC License Generator " + GENERATOR_VERSION)
        self.resize(650, 600)
        self.setMinimumSize(560, 580)
        central = QWidget()
        central.setObjectName("canvas")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(32, 28, 32, 26)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        brand = QLabel("54GUC  /  DEVELOPER TOOLS")
        brand.setObjectName("brand")
        badge = QLabel("2.0")
        badge.setObjectName("badge")
        heading.addWidget(brand)
        heading.addStretch()
        heading.addWidget(badge)
        layout.addLayout(heading)
        title = QLabel("Create an activation code")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("One device. One code. Ready to copy into 54GUC.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        card = QFrame()
        card.setObjectName("card")
        form = QVBoxLayout(card)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(10)
        form.addWidget(QLabel("INPUT  /  MODEM SERIAL OR IMEI"))
        self.serial_input = QLineEdit()
        self.serial_input.setObjectName("serial")
        self.serial_input.setAccessibleName("Modem serial or IMEI")
        self.serial_input.setPlaceholderText("Paste the device serial or IMEI")
        self.serial_input.setClearButtonEnabled(True)
        self.serial_input.returnPressed.connect(self._generate)
        self.serial_input.textChanged.connect(self._clear_output)
        form.addWidget(self.serial_input)
        self.gen_btn = QPushButton("Generate activation code")
        self.gen_btn.setObjectName("primary")
        self.gen_btn.setMinimumHeight(42)
        self.gen_btn.clicked.connect(self._generate)
        form.addWidget(self.gen_btn)
        form.addSpacing(6)
        form.addWidget(QLabel("OUTPUT  /  ACTIVATION CODE"))
        self.activation_output = QPlainTextEdit()
        self.activation_output.setObjectName("activation")
        self.activation_output.setAccessibleName("Combined nonce and licence key")
        self.activation_output.setReadOnly(True)
        self.activation_output.setPlaceholderText("Your nonce and licence key will appear here together.")
        self.activation_output.setMinimumHeight(88)
        form.addWidget(self.activation_output, 1)
        self.copy_all_btn = QPushButton("Copy activation code")
        self.copy_all_btn.setObjectName("copy")
        self.copy_all_btn.setEnabled(False)
        self.copy_all_btn.setMinimumHeight(36)
        self.copy_all_btn.clicked.connect(self._copy_all)
        form.addWidget(self.copy_all_btn)
        layout.addWidget(card, 1)
        self.status_label = QLabel("Paste a serial or IMEI to begin.")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(36)
        layout.addWidget(self.status_label)
        footer = QLabel("OFFLINE  /  " + self.tier.replace("_", " ").upper())
        footer.setObjectName("footer")
        layout.addWidget(footer)
        self.setStyleSheet("""
            QWidget#canvas { background:#f3f6fb; color:#172842; }
            QLabel { color:#405574; font:11px 'Segoe UI'; }
            QLabel#brand { color:#617493; font-weight:600; letter-spacing:1px; }
            QLabel#badge { color:#2d65d3; background:#e4edff; padding:4px 10px; border-radius:10px; font-weight:700; }
            QLabel#title { color:#142745; font:600 25px 'Segoe UI'; }
            QLabel#subtitle { color:#63758f; font:13px 'Segoe UI'; }
            QFrame#card { background:white; border:1px solid #dce4ef; border-radius:12px; }
            QLineEdit, QPlainTextEdit { background:#f9fbfe; color:#172842; border:1px solid #d6e0ed; border-radius:7px; padding:11px; font:13px 'Consolas'; selection-background-color:#d5e4ff; }
            QLineEdit:focus { border:1px solid #376de0; background:white; }
            QPlainTextEdit { background:#f2f6fc; }
            QPushButton { border-radius:7px; padding:8px 14px; font:600 13px 'Segoe UI'; }
            QPushButton#primary { background:#2861d5; color:white; border:none; }
            QPushButton#primary:hover { background:#1d51bf; }
            QPushButton#primary:pressed { background:#153f9b; }
            QPushButton#copy { background:#edf3ff; color:#245bc5; border:1px solid #d2e0fc; }
            QPushButton#copy:disabled { background:#f3f5f8; color:#8e9aab; border:1px solid #e3e8ef; }
            QLabel#footer { color:#8491a5; font:10px 'Segoe UI'; }
        """)

    def _clear_output(self):
        self.activation_output.clear()
        self.copy_all_btn.setEnabled(False)
        self.status_label.setStyleSheet("color:#63758f")
        self.status_label.setText("Ready to generate for this device." if self.serial_input.text() else "Paste a serial or IMEI to begin.")

    def _generate(self):
        self._clear_output()
        serial = self.serial_input.text().strip()
        error = validate_serial_input(serial)
        if error:
            self.status_label.setStyleSheet("color:#ad3641")
            self.status_label.setText(error)
            return
        token = generate_token(serial)
        nonce, key = generate_license(serial, tier=self.tier)
        error = validate_license_output(token, nonce, key)
        if error:
            self.status_label.setStyleSheet("color:#ad3641")
            self.status_label.setText("The activation code could not be validated. No code was copied.")
            return
        # This labelled pair is already supported by arch4.licensing.parse_license_entry.
        self.activation_output.setPlainText("Nonce: " + nonce + "\nKey:   " + key)
        self.copy_all_btn.setEnabled(True)
        self.status_label.setStyleSheet("color:#237458")
        self.status_label.setText("Ready. Copy the whole code and paste it into the 54GUC licence field.")

    def _copy_all(self):
        text = self.activation_output.toPlainText()
        if text and self.copy_all_btn.isEnabled():
            QApplication.clipboard().setText(text)
            self.status_label.setText("Activation code copied.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="54GUC License Generator 2.0")
    parser.add_argument("--tier", choices=("internal_full", "tester_trusted", "tester_basic"), default="internal_full")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        from arch4.licensing import parse_license_entry, validate_license
        for tier in ("internal_full", "tester_trusted", "tester_basic"):
            nonce, key = generate_license("867648043234567", tier=tier)
            assert validate_license("867648043234567", *parse_license_entry("Nonce: " + nonce + "\nKey: " + key))
        return
    app = QApplication(sys.argv)
    window = LicenseGenWindow(tier=args.tier)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
