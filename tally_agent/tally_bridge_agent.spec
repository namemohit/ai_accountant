# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tally_bridge_agent.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/yantrai.ico', 'assets'), ('assets/yantrai_256.png', 'assets')],
    hiddenimports=['pystray._win32', 'PIL._tkinter_finder'],
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
    name='tally_bridge_agent',
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
    icon='assets/yantrai.ico',
    version='version_info.txt',
)
