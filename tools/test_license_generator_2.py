"""Two-field developer tool: same licence owner, simpler presentation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit
from tools import guc_license_gen as generator
from arch4.licensing import parse_license_entry, validate_license, license_tier_from_nonce

@pytest.fixture
def window():
    app=QApplication.instance() or QApplication([])
    ui=generator.LicenseGenWindow()
    yield ui
    ui.close()

def test_exactly_two_fields_and_no_tier_selector(window):
    assert len(window.findChildren(QLineEdit))==1
    assert len(window.findChildren(QPlainTextEdit))==1
    assert not window.findChildren(QComboBox)
    assert window.activation_output.isReadOnly()
    assert "2.0" in window.windowTitle()

def test_combined_output_is_accepted_by_existing_activation_owner(window):
    serial="867648043234567"
    window.serial_input.setText(serial)
    window._generate()
    text=window.activation_output.toPlainText()
    nonce,key=parse_license_entry(text)
    assert validate_license(serial,nonce,key)
    assert license_tier_from_nonce(nonce)=="internal_full"
    assert "Nonce:" in text and "Key:" in text
    assert window.copy_all_btn.isEnabled()

def test_editing_identity_clears_stale_activation(window):
    window.serial_input.setText("867648043234567");window._generate()
    window.serial_input.setText("867648043234568")
    assert not window.activation_output.toPlainText()
    assert not window.copy_all_btn.isEnabled()

def test_invalid_input_has_inline_feedback_and_no_old_key(window):
    window.serial_input.setText("867648043234567");window._generate()
    window.serial_input.setText("0000");window._generate()
    assert not window.activation_output.toPlainText()
    assert "actual" in window.status_label.text().lower()
    assert not window.copy_all_btn.isEnabled()

@pytest.mark.parametrize("tier",["tester_trusted","tester_basic"])
def test_existing_tiers_remain_available_without_extra_gui_fields(tier):
    app=QApplication.instance() or QApplication([])
    ui=generator.LicenseGenWindow(tier=tier)
    try:
        ui.serial_input.setText("867648043234567");ui._generate()
        nonce,key=parse_license_entry(ui.activation_output.toPlainText())
        assert license_tier_from_nonce(nonce)==tier
        assert validate_license("867648043234567",nonce,key)
        assert len(ui.findChildren(QLineEdit))==1
    finally:ui.close()

def test_copy_uses_same_visible_combined_output(window,monkeypatch):
    copied=[]
    class Clipboard:
        def setText(self,text):copied.append(text)
    monkeypatch.setattr(generator.QApplication,"clipboard",lambda:Clipboard())
    window.serial_input.setText("867648043234567");window._generate();window._copy_all()
    assert copied==[window.activation_output.toPlainText()]

def test_generation_does_not_change_licensing_algorithm():
    assert generator.GENERATOR_VERSION=="2.0"
    assert generator._arch4_generate_license.__module__=="arch4.licensing"

def test_two_field_layout_fits_its_minimum_size(window):
    window.resize(window.minimumSize())
    window.ensurePolished()
    window.show()  # offscreen platform: activate nested layouts, no desktop input
    QApplication.processEvents()
    assert window.activation_output.geometry().bottom()<window.copy_all_btn.geometry().top()
    assert window.serial_input.geometry().bottom()<window.gen_btn.geometry().top()

def test_license_builder_uses_installed_pins_without_upgrading_environment():
    from pathlib import Path
    source=(Path(generator.__file__).parents[1]/"scripts/build_guc_license_gen_windows.ps1").read_text(encoding="utf-8-sig")
    assert "pip install --upgrade" not in source
    assert "requirements-runtime-pins.txt" in source


def test_license_freezer_uses_only_windows_system_dll_search_path():
    from pathlib import Path
    source=(Path(generator.__file__).parents[1]/"scripts/build_guc_license_gen_windows.ps1").read_text(encoding="utf-8-sig")
    assert "$previousPath = $env:PATH" in source
    assert '[Environment]::GetFolderPath("System")' in source
    assert '[Environment]::GetFolderPath("Windows")' in source
    assert "$env:PATH = $systemPaths -join [IO.Path]::PathSeparator" in source
    assert "finally" in source and "$env:PATH = $previousPath" in source
