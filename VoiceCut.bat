@echo off
setlocal
chcp 65001 >nul
title VoiceCut - GPT-SoVITS Dataset Tool

rem Project root = this script's own directory (location-independent).
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"
set "PY=%PROJ%\.venv\Scripts\python.exe"

if not exist "%PROJ%\voicecut.py" (
  echo [VoiceCut] Project not found: %PROJ%
  pause
  exit /b 1
)
if not exist "%PY%" (
  echo [VoiceCut] venv not found. Please set it up first:
  echo   cd /d "%PROJ%"
  echo   uv venv .venv --python "D:\uv-python\cpython-3.12.14-windows-x86_64-none\python.exe"
  echo   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  pause
  exit /b 1
)

cd /d "%PROJ%"
echo [VoiceCut] Starting... (close this window or press Ctrl+C to stop)
"%PY%" voicecut.py
if errorlevel 1 (
  echo.
  echo [VoiceCut] Exited with error code %errorlevel%
  pause
)
endlocal
