"""Lightweight startup acknowledgement; no runtime/modem/support imports."""
from __future__ import annotations
import os
import time
from typing import Any

ARCH_SW_LABEL = "54GUC"


def _env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _startup_notice_suppressed_by_environment() -> bool:
    if _env_flag_enabled("ARCH_DISABLE_STARTUP_NOTICE"):
        return True
    if str(os.environ.get("PYTEST_CURRENT_TEST", "") or "").strip():
        return True
    if str(os.environ.get("QT_QPA_PLATFORM", "") or "").strip().lower() == "offscreen":
        return True
    return False


class _StartupNotice:
    """Tiny best-effort window shown before slow portable startup work."""

    def __init__(self, root: Any = None, label: Any = None):
        self._root = root
        self._label = label

    @classmethod
    def show(cls, *, profile: str = "") -> "_StartupNotice":
        if _startup_notice_suppressed_by_environment():
            return cls()
        active = getattr(cls, "_active", None)
        if active is not None and active._root is not None:
            return active
        try:
            import tkinter as tk
        except Exception:
            return cls()
        try:
            root = tk.Tk()
            root.title(ARCH_SW_LABEL)
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                root.attributes("-toolwindow", True)
            except Exception:
                pass
            frame = tk.Frame(root, padx=22, pady=16, bg="#111827")
            frame.pack(fill="both", expand=True)
            title = tk.Label(
                frame,
                text=f"{ARCH_SW_LABEL} is starting",
                font=("Segoe UI", 12, "bold"),
                fg="#f9fafb",
                bg="#111827",
            )
            title.pack(anchor="w")
            profile_text = str(profile or "").strip().upper()
            detail_text = "Loading, please wait."
            if profile_text:
                detail_text = f"Loading {profile_text}, please wait."
            label = tk.Label(
                frame,
                text=detail_text,
                font=("Segoe UI", 9),
                fg="#cbd5e1",
                bg="#111827",
            )
            label.pack(anchor="w", pady=(8, 0))
            root.update_idletasks()
            width = max(320, int(root.winfo_reqwidth()))
            height = max(104, int(root.winfo_reqheight()))
            screen_w = int(root.winfo_screenwidth() or 0)
            screen_h = int(root.winfo_screenheight() or 0)
            x = max(0, int((screen_w - width) / 2)) if screen_w else 120
            y = max(0, int((screen_h - height) / 3)) if screen_h else 120
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.update()
            shown = cls(root, label)
            cls._active = shown
            os.environ["ARCH_STARTUP_NOTICE_VISIBLE_AT"] = str(time.time())
            return shown
        except Exception:
            try:
                root.destroy()
            except Exception:
                pass
            return cls()

    def pulse(self, message: str = "") -> None:
        root = self._root
        if root is None:
            return
        try:
            if message and self._label is not None:
                self._label.configure(text=str(message))
            root.update_idletasks()
            root.update()
        except Exception:
            pass

    def close(self) -> None:
        root = self._root
        if getattr(type(self), "_active", None) is self:
            type(self)._active = None
        self._root = None
        self._label = None
        if root is None:
            return
        try:
            root.destroy()
        except Exception:
            pass

