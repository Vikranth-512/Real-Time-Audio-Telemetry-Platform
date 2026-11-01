# AudioAnalyzer.spec
# ------------------
# PyInstaller --onedir spec for the Audio Analyzer desktop application.
#
# Build:
#   pyinstaller AudioAnalyzer.spec
#
# Output: dist/AudioAnalyzer/AudioAnalyzer.exe
#
# Notes:
#   - PySide6 is auto-discovered via hiddenimports.
#   - sounddevice bundles its own PortAudio DLL; no extra binaries needed.
#   - The backend/processing directory is included as a package so that
#     metrics_engine.py is accessible at runtime without modifying sys.path.
#   - data/sessions/ is NOT bundled — it is written at runtime next to the exe.

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['app/main.py'],
    pathex=[
        str(Path('.').resolve()),
        str(Path('backend').resolve()),
    ],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PySide6',
        'PySide6.QtWidgets',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'pyqtgraph',
        'pyaudiowpatch',
        'numpy',
        'scipy',
        'pandas',
        'pyarrow',
        'pyarrow.parquet',
        'serial',
        'serial.tools.list_ports',
        'processing.metrics_engine',   # backend package reference
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy unused PySide6 modules to reduce bundle size
        'PySide6.QtWebEngine',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtMultimedia',
        'PySide6.Qt3DCore',
        # Exclude all web/server dependencies (no longer needed)
        'fastapi',
        'uvicorn',
        'redis',
        'sqlalchemy',
        'asyncpg',
        'aioredis',
        'websockets',
        'starlette',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioAnalyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # No console window — native desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # Add icon path here when available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AudioAnalyzer',
)
