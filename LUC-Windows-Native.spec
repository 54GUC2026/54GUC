# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


CGF_HIDDEN_IMPORT_PACKAGES = (
    "arch4.b818",
    "arch4.control",
    "arch4.core",
    "arch4.core_contracts",
    "arch4.customer_contracts",
    "arch4.foundation",
    "arch4.modem",
    "arch4.modem_live",
    "arch4.ngui",
    "arch4.new_horizon",
    "arch4.policy",
    "arch4.signal_processing",
    "arch4.startup",
)

DETACHED_CUSTOMER_EXCLUDES = (
    "arch4.brain",
    "arch4.intelligence",
    "arch4.smart_loop",
)


def _is_detached_customer_module(name):
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in DETACHED_CUSTOMER_EXCLUDES
    )


def _include_cgf_hiddenimport(name):
    if name.startswith("arch4.tests") or _is_detached_customer_module(name):
        return False
    return True


def _include_cgf_binary(entry):
    # The GUI hook collects an unused GPL-only virtual keyboard that
    # this application does not use. Preserve normal Windows input.
    name = entry[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name not in {"qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll"}


arch4_hiddenimports = []
for package in CGF_HIDDEN_IMPORT_PACKAGES:
    arch4_hiddenimports.extend(
        collect_submodules(package, filter=_include_cgf_hiddenimport)
    )
arch4_hiddenimports = sorted(set(arch4_hiddenimports))


a = Analysis(
    ["arch4\\windows_native_launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("arch4/ngui/fonts/*.ttf", "arch4/ngui/fonts"),
        ("arch4/ngui/fonts/LICENSE-SIL-OFL.txt", "arch4/ngui/fonts"),
        ("config/*.json", "config"),
        ("config/modems/*.env", "config/modems"),
        ("config/ip/*.txt", "config/ip"),
        ("open_source_manifest.json", "."),
        ("third_party/licenses", "licenses"),
        ("third_party/NOTICE.txt", "."),
        ("LICENSE", "."),
        ("LICENSING.md", "."),
        ("arch4/modem_live/profile_definition/*.json", "arch4/modem_live/profile_definition"),
        ("arch4/modem_live/profile_definition/codecs/*.json", "arch4/modem_live/profile_definition/codecs"),
    ],
    hiddenimports=[
        *arch4_hiddenimports,
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtSvg",
        "pyqtgraph",
        "shiboken6",
        "requests",
        "urllib3",
        "idna",
        "charset_normalizer",
        "certifi",
        "routeros_api",
        "regex",
        "regex._regex",
        "routeros_api.api",
        "routeros_api.api_communicator",
        "routeros_api.api_communicator.async_decorator",
        "routeros_api.api_communicator.base",
        "routeros_api.api_communicator.encoding_decorator",
        "routeros_api.api_communicator.exception_decorator",
        "routeros_api.api_communicator.key_cleaner_decorator",
        "routeros_api.api_socket",
        "routeros_api.api_structure",
        "routeros_api.base_api",
        "routeros_api.communication_exception_parsers",
        "routeros_api.exceptions",
        "routeros_api.query",
        "routeros_api.resource",
        "routeros_api.sentence",
        "routeros_api.utils",
    ],
    # The local plotting hook keeps normal imports available; the build stage
    # removes the runtime worker hook that would start a second GUI process.
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        *DETACHED_CUSTOMER_EXCLUDES,
    ],
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if _include_cgf_binary(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="54GUC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="54GUC",
)
