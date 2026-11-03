@echo off
rem ===================================================================
rem  Remote access helper - makes the app reachable from ANY network,
rem  not just the same WiFi. Real work happens in scripts\setup_remote.py
rem
rem  What it does, in order:
rem    1. checks Tailscale is installed (if not, tells you which file to
rem       double-click: tools\tailscale-setup.exe)
rem    2. walks you through logging in (it prints a login URL)
rem    3. sets up `tailscale serve` so the phone gets an https:// address
rem       (private to your own devices - NEVER funnel)
rem
rem  Keep this file pure ASCII too - see the long note in start.bat.
rem  Tip: right-click -> "Run as administrator" if it complains about
rem  permissions (tailscale.exe needs it to talk to the service).
rem ===================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" "scripts\setup_remote.py" %*

pause
