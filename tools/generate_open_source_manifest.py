#!/usr/bin/env python3
"""Generate the sole VERSION-dialog open-source dependency manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from arch4.open_source_manifest import OWNER, SCHEMA, component_fingerprint


CATALOG: tuple[dict[str, str], ...] = (
    {"name":"regex","license":"Apache-2.0 AND CNRI-Python","use":"time-bounded editable profile patterns","category":"runtime","evidence":"requirement:regex"},
    {
        "name": "Python",
        "license": "PSF License 2.0",
        "use": "application runtime",
        "category": "runtime",
        "evidence": "runtime",
    },
    {
        "name": "PySide6",
        "license": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
        "use": "Qt Python bindings",
        "category": "runtime",
        "evidence": "requirement:PySide6",
    },
    {
        "name": "Qt",
        "license": "LGPL-3.0/GPL/commercial",
        "use": "GUI framework",
        "category": "runtime",
        "evidence": "dependency:PySide6",
    },
    {
        "name": "Shiboken6",
        "license": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
        "use": "Qt binding runtime",
        "category": "runtime",
        "evidence": "dependency:PySide6",
    },
    {
        "name": "pyqtgraph",
        "license": "MIT",
        "use": "live charts",
        "category": "runtime",
        "evidence": "requirement:pyqtgraph",
    },
    {
        "name": "NumPy",
        "license": "BSD-3-Clause and bundled component licenses",
        "use": "numeric and chart processing",
        "category": "runtime",
        "evidence": "dependency:pyqtgraph",
    },
    {
        "name": "requests",
        "license": "Apache-2.0",
        "use": "HTTP client",
        "category": "runtime",
        "evidence": "requirement:requests",
    },
    {
        "name": "urllib3",
        "license": "MIT",
        "use": "HTTP transport",
        "category": "runtime",
        "evidence": "dependency:requests",
    },
    {
        "name": "certifi",
        "license": "MPL-2.0",
        "use": "CA certificate bundle",
        "category": "runtime",
        "evidence": "dependency:requests",
    },
    {
        "name": "charset-normalizer",
        "license": "MIT",
        "use": "HTTP text decoding",
        "category": "runtime",
        "evidence": "dependency:requests",
    },
    {
        "name": "idna",
        "license": "BSD-3-Clause",
        "use": "internationalized host names",
        "category": "runtime",
        "evidence": "dependency:requests",
    },
    {
        "name": "cryptography",
        "license": "Apache-2.0 OR BSD-3-Clause",
        "use": "Huawei APN request encryption",
        "category": "runtime",
        "evidence": "requirement:cryptography",
    },
    {
        "name": "cffi",
        "license": "MIT-0",
        "use": "cryptography native bindings",
        "category": "runtime",
        "evidence": "dependency:cryptography",
    },
    {
        "name": "pycparser",
        "license": "BSD-3-Clause",
        "use": "CFFI parser dependency",
        "category": "runtime",
        "evidence": "dependency:cffi",
    },
    {
        "name": "RouterOS-api",
        "license": "MIT",
        "use": "MikroTik RouterOS API client",
        "category": "runtime",
        "evidence": "requirement:routeros_api",
    },
    {
        "name": "Liberation Mono",
        "license": "SIL Open Font License 1.1",
        "use": "bundled UI font",
        "category": "runtime_asset",
        "evidence": "path:arch4/ngui/fonts/LICENSE-SIL-OFL.txt",
    },
    {
        "name": "OpenSSL",
        "license": "Apache-2.0",
        "use": "TLS in the Python runtime",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "SQLite",
        "license": "Public Domain",
        "use": "runtime database support",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "zlib",
        "license": "Zlib",
        "use": "compressed runtime resources",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "bzip2",
        "license": "BSD-like bzip2 license",
        "use": "Python compression support",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "XZ Utils / liblzma",
        "license": "Public Domain and LGPL components",
        "use": "Python compression support",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "libffi",
        "license": "MIT",
        "use": "Python foreign-function support",
        "category": "runtime_component",
        "evidence": "python_runtime",
    },
    {
        "name": "PyInstaller",
        "license": "GPL-2.0-or-later with bootloader exception",
        "use": "Windows packaging",
        "category": "build",
        "evidence": "path:LUC-Windows-Native.spec",
    },
    {
        "name": "pywin32",
        "license": "PSF",
        "use": "Windows integration and inspection",
        "category": "development",
        "evidence": "source_import",
    },
    {
        "name": "comtypes",
        "license": "MIT",
        "use": "Windows COM inspection",
        "category": "development",
        "evidence": "source_import",
    },
    {
        "name": "pywinauto",
        "license": "BSD-3-Clause",
        "use": "Windows UI verification",
        "category": "development",
        "evidence": "source_import",
    },
    {
        "name": "PyAutoGUI",
        "license": "BSD-3-Clause",
        "use": "Windows UI verification",
        "category": "development",
        "evidence": "source_import",
    },
    {
        "name": "py-spy",
        "license": "MIT",
        "use": "runtime diagnostics",
        "category": "development",
        "evidence": "tooling",
    },
    {
        "name": "Pillow",
        "license": "MIT-CMU",
        "use": "visual comparison tooling",
        "category": "development",
        "evidence": "requirement_file:requirements-ngui-visual.txt",
    },
    {
        "name": "OpenCV Python",
        "license": "Apache-2.0",
        "use": "visual comparison tooling",
        "category": "development",
        "evidence": "requirement_file:requirements-ngui-visual.txt",
    },
    {
        "name": "pytesseract",
        "license": "Apache-2.0",
        "use": "visual OCR tooling",
        "category": "development",
        "evidence": "requirement_file:requirements-ngui-visual.txt",
    },
    {
        "name": "Tesseract OCR",
        "license": "Apache-2.0",
        "use": "visual OCR engine",
        "category": "development",
        "evidence": "external_tool",
    },
    {
        "name": "pytest",
        "license": "MIT",
        "use": "test suite",
        "category": "development",
        "evidence": "source_import",
    },
    {
        "name": "Ruff",
        "license": "MIT",
        "use": "source linting",
        "category": "development",
        "evidence": "tooling",
    },
)


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        # pip constraints set versions; they do not declare new dependencies.
        if raw.startswith(("-c ", "--constraint ")):
            continue
        name = re.split(r"[<>=!~\[]", raw, maxsplit=1)[0].strip().lower()
        names.add(name.replace("-", "_"))
    return names


def build_manifest(repo_root: Path) -> dict[str, Any]:
    requirements = _requirement_names(repo_root / "requirements.txt")
    catalog_requirements = {
        row["evidence"].split(":", 1)[1].lower().replace("-", "_")
        for row in CATALOG
        if row["evidence"].startswith("requirement:")
    }
    missing = sorted(requirements - catalog_requirements)
    if missing:
        raise RuntimeError(
            "open_source_manifest_missing_runtime_requirements:" + ",".join(missing)
        )
    components = [dict(row) for row in CATALOG]
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "catalog_sha256": component_fingerprint(components),
        "generated_from": [
            "requirements.txt",
            "requirements-ngui-visual.txt",
            "LUC-Windows-Native.spec",
            "source and tooling imports",
        ],
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", default="open_source_manifest.json")
    args = parser.parse_args()
    root = Path(args.repo_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    payload = build_manifest(root)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
