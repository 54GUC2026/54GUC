# -*- mode: python ; coding: utf-8 -*-
"""Freezer specification for the standalone activation-code generator.

Use scripts/build_guc_license_gen_windows.ps1 from the repository root.
The build coordinator chooses and verifies the environment and output folder;
do not bypass it by invoking this specification directly.
"""

a = Analysis(
    ['tools/guc_license_gen.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'tkinter', 'unittest',
    ],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GUC_License_Gen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if desired: icon='guc_icon.ico'
)
