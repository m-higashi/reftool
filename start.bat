@echo off
chcp 65001 >nul
rem =====================================================================
rem  Reference tool launcher (Windows)
rem  Double-click to start. Access URLs are printed by run.py below.
rem  Stop: close this window or press Ctrl+C.
rem
rem  RULE 1: This file must contain ASCII characters ONLY (no Japanese).
rem    cmd.exe mis-splits lines containing multibyte UTF-8 text when the
rem    batch runs in a fresh console (double-click / Start-Process),
rem    regardless of chcp position. Fragments of comments then get
rem    executed as commands ("'...' is not recognized ..." noise).
rem    Japanese messages belong in run.py (Python prints them fine).
rem  RULE 2: No ( or ) inside echo text within if-blocks.
rem =====================================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

rem --- Detect a broken .venv, e.g. folder copied from another PC -------
rem  A venv is not portable: pyvenv.cfg has the old PC's Python path
rem  baked in. If python.exe exists but cannot run, rebuild the venv.
if exist "%PY%" (
    "%PY%" --version >nul 2>nul
    if errorlevel 1 (
        echo [setup] Existing .venv is broken - copied from another PC? Rebuilding...
        rmdir /s /q .venv
    )
)

rem --- First run / after rebuild: create venv and install deps ---------
if not exist "%PY%" (
    echo [setup] First run: creating virtual environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -m venv .venv
    ) else (
        python -m venv .venv
    )
    if not exist "%PY%" (
        echo [error] Python not found. Please install Python 3.11 or later.
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
)

rem --- Choose interpreter ----------------------------------------------
rem Windows Firewall block rules are matched by the executable's path, so a
rem rule blocking the venv's python.exe silently stops other devices (LAN or
rem Tailscale) from connecting while localhost still works. If the Python
rem launcher alias exists, run through it instead and point PYTHONPATH at the
rem venv's packages. If it does not exist, the venv's python is used as usual.
set "RUNPY=%PY%"
if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
    set "RUNPY=%LOCALAPPDATA%\Python\bin\python.exe"
    set "PYTHONPATH=%~dp0.venv\Lib\site-packages"
)

echo.
"%RUNPY%" run.py

echo.
echo Server stopped. You can close this window.
pause
