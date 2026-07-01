# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


# spec 已归档到“打包配置”目录，因此项目根目录需要回到上一级。
spec_dir = Path(SPECPATH)
project_dir = spec_dir.parent
datas = [
    (str(project_dir / "recover_wxwork_partial_messages.py"), "."),
    (str(project_dir / "organize_wxwork_recovered_messages.py"), "."),
    (str(project_dir / "read_wxwork_encrypted_databases.py"), "."),
]

bundled_docs = [
    project_dir / "文档" / "使用说明" / "WXWork_Recovery_GUI_Guide.md",
    project_dir / "文档" / "使用说明" / "WXWorkRecoveryGUI_按钮功能详解.md",
    project_dir / "文档" / "交接资料" / "WXWork_Chat_Recovery_Playbook.md",
    project_dir / "文档" / "交接资料" / "WXWork_Recovery_Codex_Handoff.md",
    project_dir / "文档" / "设计资料" / "项目详细链路说明-聊天恢复与知识库接入.md",
]

for doc_path in bundled_docs:
    if doc_path.exists():
        datas.append((str(doc_path), "."))


a = Analysis(
    [str(project_dir / "wxwork_recovery_gui.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "recover_wxwork_partial_messages",
        "organize_wxwork_recovered_messages",
        "read_wxwork_encrypted_databases",
        "PIL",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageTk",
        "pymem",
        "psutil",
        "sqlite3",
        "_sqlite3",
        "argparse",
        "binascii",
        "csv",
        "ctypes",
        "json",
        "struct",
    ],
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
    name="WXWorkRecoveryGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
