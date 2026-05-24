@echo off
REM ════════════════════════════════════════════════════════════════════════════
REM  AudioCaptureAgent — PyInstaller Build Script
REM  Run from within the audio_capture_agent\ directory.
REM
REM  Prerequisites:
REM    pip install -r requirements.txt
REM
REM  Output:
REM    dist\AudioCaptureAgent.exe
REM ════════════════════════════════════════════════════════════════════════════

setlocal ENABLEEXTENSIONS

echo.
echo ┌────────────────────────────────────────────────────────────┐
echo │          AudioCaptureAgent — Production Build              │
echo └────────────────────────────────────────────────────────────┘
echo.

REM ── Step 1: Ensure dependencies are installed ─────────────────────────────
echo [1/3] Installing Python dependencies...
py -3.11 -m pip install -r requirements.txt
if ERRORLEVEL 1 (
    echo ERROR: pip install failed. Aborting.
    pause
    exit /b 1
)

REM ── Step 2: Build the executable ─────────────────────────────────────────
echo [2/3] Running PyInstaller...

REM Determine if icon.ico exists
set ICON_FLAG=
if exist icon.ico (
    set ICON_FLAG=--icon icon.ico
)

py -3.11 -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --name AudioCaptureAgent ^
    %ICON_FLAG% ^
    --hidden-import aiohttp ^
    --hidden-import websockets ^
    --hidden-import pyaudio ^
    --hidden-import numpy ^
    --hidden-import orjson ^
    --collect-all aiohttp ^
    agent.py

if ERRORLEVEL 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

REM ── Step 3: Verify output ─────────────────────────────────────────────────
echo [3/3] Verifying output...
if exist dist\AudioCaptureAgent.exe (
    echo.
    echo ✔  Build successful!
    echo    Output: %CD%\dist\AudioCaptureAgent.exe
    echo.
    echo Next steps:
    echo   1. Copy dist\AudioCaptureAgent.exe to "C:\Program Files\AudioCaptureAgent\"
    echo   2. Double-click register_protocol.reg to register the audioanalyzer:// handler
    echo   3. Serve dist\AudioCaptureAgent.exe from your backend at /downloads/AudioCaptureAgent.exe
) else (
    echo ERROR: dist\AudioCaptureAgent.exe not found after build.
    exit /b 1
)

echo.
pause
endlocal
