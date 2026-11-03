@echo off
rem ===================================================================
rem  Phone access helper - prints the LAN URL and writes a QR image.
rem  Real work happens in scripts\phone_qr.py.
rem  Keep this file pure ASCII - see the note in start.bat.
rem ===================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" "scripts\phone_qr.py" --open %*

pause
