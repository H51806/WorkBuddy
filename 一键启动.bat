@echo off
title WorkBuddy Manager
setlocal

rem ============================================================
rem  WorkBuddy Manager -- the ONLY launcher you need.
rem  (ASCII-only on purpose: cmd.exe mis-parses non-ASCII .bat)
rem ============================================================

rem -- locate main.py: this folder first (repo layout), then a WorkBuddy_* child --
set "APPDIR="
if exist "%~dp0main.py" set "APPDIR=%~dp0"
if not defined APPDIR for /d %%d in ("%~dp0WorkBuddy_*") do if exist "%%d\main.py" set "APPDIR=%%d"
if not defined APPDIR (echo [ERROR] main.py not found: neither in this folder nor in a WorkBuddy_* subfolder. & pause & exit /b 1)
cd /d "%APPDIR%"
if not exist "main.py" (echo [ERROR] main.py not found in "%APPDIR%". & pause & exit /b 1)

rem -- find a real CPython 3.10+ (avoid the Microsoft Store stub) --
set "PYTHON_CMD="
where.exe py >nul 2>&1
if %ERRORLEVEL%==0 for /f "usebackq delims=" %%p in (`py -3 -c "import sys; print(sys.executable)"`) do if not defined PYTHON_CMD set "PYTHON_CMD=%%p"
for %%v in (316 315 314 313 312 311 310) do if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python%%v\python.exe" set "PYTHON_CMD=%LocalAppData%\Programs\Python\Python%%v\python.exe"
for %%v in (316 315 314 313 312 311 310) do if not defined PYTHON_CMD if exist "C:\Python%%v\python.exe" set "PYTHON_CMD=C:\Python%%v\python.exe"
if not defined PYTHON_CMD (echo [ERROR] No Python 3.10+ found. Install from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)

echo ============================================
echo  WorkBuddy Manager
echo ============================================
echo  Panel (main): http://127.0.0.1:7863/panel/
echo  API Gateway : http://127.0.0.1:7863
echo  Legacy UI   : http://localhost:8789  (auto-redirects to Panel)
echo  python      : %PYTHON_CMD%
echo.
echo  Keep this window OPEN while WorkBuddy runs.
echo  Press Ctrl+C in this window to stop.
echo ============================================
echo.

"%PYTHON_CMD%" -u main.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo WorkBuddy Manager stopped with code %EXITCODE%.
echo If it stopped unexpectedly, keep this window open and send the text above.
pause
exit /b %EXITCODE%
