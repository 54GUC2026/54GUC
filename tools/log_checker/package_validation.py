"""Native checker packaging proof, with no modem access or CGF termination."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

from tools.log_checker.app import cgf_running
from tools.log_checker.report import VERSION


def validate(build, notices, output):
    # This specific guard test requires an already running portable. It does
    # not start one or stop one. The self-tests read synthetic data only.
    assert cgf_running(), "The busy-app guard proof requires an existing CGF run"
    output.mkdir(parents=True, exist_ok=False)
    exe = build / "54GUC_Log_Checker.exe"
    subprocess.run([str(exe), "--self-test", str(output / "original_smoke")], check=True, timeout=60)
    package = output / "package"
    package.mkdir()
    copied = package / exe.name
    shutil.copy2(exe, copied)
    shutil.copy2(build / "LOG_CHECKER_READ_FIRST.txt", package)
    subprocess.run([str(copied), "--self-test", str(output / "copied_smoke")], check=True, timeout=60)
    fixture = output / "busy_fixture"
    fixture.mkdir()
    (fixture / "54GUC.exe").write_bytes(b"Marker for guard test; never executable")
    result = subprocess.run([str(copied), "--app-dir", str(fixture), "--check"], timeout=60)
    assert result.returncode == 3, f"Live CGF must prevent review: got {result.returncode}"
    assert not list(fixture.rglob("report.*")), "A review started while CGF was running"
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    title = "54GUC Log Checker " + VERSION
    assert not user.FindWindowW("TkTopLevel", title), "Another checker window is already open"
    window = subprocess.Popen([str(copied), "--app-dir", str(fixture)])
    handle = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and window.poll() is None:
        handle = user.FindWindowW("TkTopLevel", title)
        if handle:
            break
        time.sleep(.25)
    assert handle, "Frozen Tk window did not appear"
    # Let the wait loop run while CGF stays active. Close only this new checker.
    time.sleep(4)
    assert not list(fixture.rglob("report.*")), "Waiting window started analysis of live CGF"
    user.PostMessageW(handle, 0x10, 0, 0)
    assert window.wait(timeout=30) == 0
    runtime = package / "CHECKER_NOTICES"
    runtime.mkdir()
    for name in ("Python-LICENSE.txt", "OpenSSL-LICENSE.txt", "Tcl-LICENSE.txt", "libffi-LICENSE.txt", "zlib-LICENSE.txt"):
        shutil.copy2(notices / "runtime" / name, runtime)
    shutil.copy2(notices / "python_packages/pyinstaller-6.22.3.dist-info/licenses/COPYING.txt.txt", runtime / "PyInstaller-COPYING.txt")
    (runtime / "READ_FIRST.txt").write_text(
        "This independent checker includes Python, its standard runtime components, Tcl/Tk, and the PyInstaller bootloader.\n"
        "Their notices are kept here. The PyInstaller notice includes its bootloader exception.\n"
        "The checker uses Tk; Qt and PySide6 are not included. No Python installation or paid dependency is required.\n", encoding="utf-8")
    archive = output / "54GUC_Log_Checker.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(package))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert len(z.namelist()) == 9
        assert not any("history" in n.lower() or "report.json" in n for n in z.namelist())
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = dict(original_frozen_report=True, copied_frozen_report=True, busy_app_refused=True, frozen_wait_window=True,
                   cgf_still_running=cgf_running(), archive_bytes=archive.stat().st_size,
                   archive_sha256=archive_hash,
                   source_build=json.loads((build / "build_receipt.json").read_text(encoding="utf-8-sig")))
    (output / "validation_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate(args.build, args.notices, args.output)
