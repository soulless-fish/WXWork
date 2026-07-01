# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


# spec 已归档到“打包配置”目录，因此项目根目录需要回到上一级。
project_dir = Path(SPECPATH).parent


a = Analysis(
    [str(project_dir / "recover_wxwork_partial_messages.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=["pymem", "psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WXWorkRecoveryCli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
