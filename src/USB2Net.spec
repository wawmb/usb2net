# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

try:
    _spec_dir = os.path.dirname(os.path.abspath(SPEC))
except NameError:
    # 非 PyInstaller 注入环境（罕见）时，回退到当前目录（build.bat / CI 均先 cd 到 src/）
    _spec_dir = os.getcwd()

hiddenimports = ['websockets.asyncio.server', 'websockets.asyncio.client', 'serial', 'serial.tools.list_ports']
hiddenimports += collect_submodules('websockets')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('static', 'static'), ('USB2Net.ico', '.'), ('USB2Net.png', '.')],
    hiddenimports=hiddenimports,
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
    name='USB2Net',
    icon=os.path.join(_spec_dir, 'USB2Net.ico'),
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
