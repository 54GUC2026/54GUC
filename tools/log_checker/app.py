"""Small independent Windows window; it never starts or stops 54GUC."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

from tools.log_checker.report import AppRunning, VERSION, analyse, analyse_bundle, blank_report, write_report


def cgf_running():
    """Fail closed if process inspection fails. No shell or modem access."""
    if os.name != "nt":
        raise OSError("Live process detection is supported on Windows only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        while ok:
            if entry.szExeFile.lower() == "54guc.exe":
                return True
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):
            raise ctypes.WinError(ctypes.get_last_error())
        return False
    finally:
        kernel.CloseHandle(handle)


class SingleChecker:
    """OS-held lock released even if the checker is closed or crashes."""
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / ".checker.lock").open("a+b")
        try:
            self.stream.seek(0)
            if not self.stream.read(1):
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Another checker is already using this folder") from None

    def close(self):
        self.stream.close()


def report_destination(root):
    return root / "log_analysis" / ("review_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))


def run_check(root, history=None):
    if root.is_file() and root.suffix.lower() == ".zip":
        report = analyse_bundle(root)
        return write_report(report, report_destination(root.parent))
    report = analyse(root, history_path=history, running=cgf_running)
    if cgf_running():
        raise AppRunning("54GUC restarted. Waiting for it to close.")
    return write_report(report, report_destination(root))


def window(root, history=None):
    import tkinter as tk
    from tkinter import messagebox
    ui = tk.Tk()
    ui.title("54GUC Log Checker " + VERSION)
    ui.geometry("690x310")
    ui.minsize(560, 260)
    is_bundle = root.is_file() and root.suffix.lower() == ".zip"
    try:
        lease = SingleChecker((root.parent if is_bundle else root) / "log_analysis")
    except (OSError, RuntimeError) as exc:
        messagebox.showerror("Cannot start", str(exc))
        ui.destroy()
        return 2
    status = tk.StringVar(value="Reading frozen support ZIP…" if is_bundle else "Checking whether 54GUC is still running…")
    tk.Label(ui, text="54GUC Log Checker", font=("Segoe UI", 19, "bold")).pack(pady=(20, 8))
    tk.Label(ui, textvariable=status, wraplength=630, justify="left", font=("Segoe UI", 11)).pack(padx=25, pady=10)
    tk.Label(ui, text=("This reads the frozen ZIP only. No app, modem or History changes." if is_bundle else
        "Leave this window open, then close 54GUC normally.\nThe report starts automatically. No modem commands or history changes."), wraplength=630, justify="left").pack(padx=25, pady=8)
    events = queue.Queue()
    state = dict(idle=0, busy=False, report=None, finished=False)
    def open_report():
        if state["report"]:
            try:
                os.startfile(str(state["report"]))
            except OSError:
                status.set("Review saved. Open report.html in the app's log_analysis folder.")
    button = tk.Button(ui, text="Open report", command=open_report, state="disabled")
    button.pack(pady=10)
    def work():
        try:
            events.put(("done", run_check(root, history)))
        except AppRunning as exc:
            events.put(("wait", str(exc)))
        except Exception as exc:
            # Do not show unsanitized credential-bearing input or stack data.
            events.put(("error", "The review could not finish (" + type(exc).__name__ + "). No app or History files were changed. Check folder access and try again."))
    def poll():
        try:
            kind, value = events.get_nowait()
            state["busy"] = False
            if kind == "done":
                state.update(report=value, finished=True)
                status.set("Review complete. Open the report for plain-English findings and Brain decisions in the saved review period.\nSaved in: " + str(value.parent))
                button.config(state="normal")
                open_report()
            elif kind == "error":
                state["finished"] = True
                status.set(value)
            else:
                state["idle"] = 0
                status.set(value)
        except queue.Empty:
            pass
        if not state["busy"] and not state["finished"]:
            try:
                if not is_bundle and cgf_running():
                    state["idle"] = 0
                    status.set("Waiting: 54GUC is still running. No log analysis has started.")
                else:
                    state["idle"] += 1
                    status.set("Frozen support ZIP ready." if is_bundle else "54GUC has closed. Waiting briefly for its files to settle…")
                    if state["idle"] >= 3:
                        state["busy"] = True
                        status.set("Reading the saved logs and History. This can take a few minutes. The original files are read-only.")
                        threading.Thread(target=work, daemon=True).start()
            except OSError:
                state["idle"] = 0
                status.set("Windows could not confirm that 54GUC has closed. No analysis will start until that check succeeds.")
        ui.after(1000, poll)
    ui.after(100, poll)
    ui.mainloop()
    lease.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent offline 54GUC log checker")
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--bundle", type=Path, help="Read a frozen support ZIP directly; no app installation required")
    parser.add_argument("--history", type=Path, help="Explicit stopped History store; no search or merge")
    parser.add_argument("--check", action="store_true", help="Check now; refuse if 54GUC is running")
    parser.add_argument("--self-test", type=Path, help="Write a synthetic report here, without reading an app or modem")
    args = parser.parse_args(argv)
    if args.self_test:
        report = blank_report(time.time())
        report["summary"] = "Synthetic package test only; no modem or real logs were read."
        report["hourly"] = []
        write_report(report, args.self_test)
        return 0
    if args.bundle:
        root = args.bundle.resolve()
        if not root.is_file() or root.suffix.lower() != ".zip" or args.app_dir or args.history:
            return 2
        if not args.check:
            return window(root)
        try:
            run_check(root)
            return 0
        except Exception as exc:
            if sys.stderr is not None:
                print("Support ZIP review could not finish (" + type(exc).__name__ + "). Original data was not changed.", file=sys.stderr)
            return 2
    root = (args.app_dir or (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd())).resolve()
    if not (root / "54GUC.exe").is_file():
        if not args.check:
            import tkinter.messagebox
            tkinter.messagebox.showerror("App folder missing", "Place 54GUC_Log_Checker.exe beside 54GUC.exe, then open it again.")
        return 2
    if args.check:
        with_error = 0
        lease = None
        try:
            lease = SingleChecker(root / "log_analysis")
            run_check(root, args.history)
        except AppRunning:
            with_error = 3
        except Exception as exc:
            if sys.stderr is not None:
                print("Review could not finish (" + type(exc).__name__ + "). Check folder access. Original data was not changed.", file=sys.stderr)
            with_error = 2
        finally:
            if lease is not None:
                lease.close()
        return with_error
    return window(root, args.history)


if __name__ == "__main__":
    raise SystemExit(main())
