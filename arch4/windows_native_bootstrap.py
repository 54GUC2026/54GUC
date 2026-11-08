"""Frozen GUI entry: acknowledge first, then load the application owner."""
from __future__ import annotations

import importlib
import multiprocessing
import sys

from arch4.startup_notice import _StartupNotice

_OFFLINE_FLAGS = frozenset({"-h", "--help", "--print-launch-plan", "--portable-validate",
    "--create-support-bundle", "--open-logs", "--profile-tools"})


def main(argv=None):
    # A frozen worker dispatches before any UI, runtime import or lease work.
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    notice = None
    try:
        if not any(arg.split("=", 1)[0] in _OFFLINE_FLAGS for arg in args):
            notice = _StartupNotice.show()
        return importlib.import_module("arch4.windows_native_launcher").main(args)
    finally:
        if notice is not None:
            notice.close()


if __name__ == "__main__":
    raise SystemExit(main())
