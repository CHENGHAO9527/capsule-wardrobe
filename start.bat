@echo off
rem ===================================================================
rem  Capsule Wardrobe - launcher (this file MUST stay pure ASCII)
rem
rem  Why no Chinese text in here:
rem  cmd.exe parses a .bat file line by line using the OEM codepage.
rem  A multi-byte character (Chinese) in the file, combined with a
rem  chcp switch, makes cmd lose its read position inside the file.
rem  The result is a screen full of
rem      "'xxx' is not recognized as an internal or external command"
rem  All user-facing text lives in scripts\serve.py instead.
rem ===================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem Prefer the managed venv; fall back to whatever python is on PATH.
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"

"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [ERROR] Could not find a Python with fastapi + uvicorn.
  echo   Tried: %PY%
  echo.
  echo   Install once with:
  echo     python -m pip install -r requirements.txt
  echo   If your python lives elsewhere, edit the PY line in start.bat
  echo   or in start-lan.bat's target.
  echo   See README.md - section "Startup".
  echo.
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" "scripts\serve.py" %*
pause
