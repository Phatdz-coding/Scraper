# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['entrypoint.py'],
    pathex=['src'],
    binaries=[],
    datas=[('src/scrape/scripts', 'scrape/scripts')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='scrape',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)