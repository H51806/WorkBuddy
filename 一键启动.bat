@echo off
title WorkBuddy Manager
cd /d "%~dp0"

echo ============================================
echo WorkBuddy Manager
echo ============================================
echo.
echo API Gateway : http://127.0.0.1:7863
echo Panel       : http://127.0.0.1:7863/panel/
echo Web UI      : http://localhost:8789
echo.
echo Keep this window open while WorkBuddy is running.
echo Press Ctrl+C in this window to stop.
echo.
echo ============================================
echo.

REM --- Locate Python 3.10+ ---
REM Order matters: `where.exe python` on Windows commonly returns the broken
REM Windows Store stub (Microsoft\WindowsApps\python.exe), so we resolve the
REM real install FIRST via the `py -3` launcher, then explicit install paths.
set "PYTHON_CMD="
set "PYTHON_PATH="

REM 1) Prefer the Python launcher (`py -3`), which is present whenever a real
REM    CPython is installed via python.org. It resolves the highest installed
REM    3.x and is not fooled by the Store stub.
where.exe py >nul 2>&1
if %ERRORLEVEL%==0 (
  for /f "usebackq delims=" %%p in (`py -3 -c "import sys; print(sys.executable)"`) do (
    if not defined PYTHON_PATH set "PYTHON_PATH=%%p"
  )
)

REM 2) Fall back to common install locations (Windows per-user layout, then C:\).
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python315\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python315\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python314\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python314\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python313\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python312\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python311\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "%LocalAppData%\Programs\Python\Python310\python.exe" (
    set "PYTHON_PATH=%LocalAppData%\Programs\Python\Python310\python.exe"
  )
)
if not defined PYTHON_PATH (
  if exist "C:\Python315\python.exe" set "PYTHON_PATH=C:\Python315\python.exe"
)
if not defined PYTHON_PATH (
  if exist "C:\Python314\python.exe" set "PYTHON_PATH=C:\Python314\python.exe"
)
if not defined PYTHON_PATH (
  if exist "C:\Python313\python.exe" set "PYTHON_PATH=C:\Python313\python.exe"
)
if not defined PYTHON_PATH (
  if exist "C:\Python312\python.exe" set "PYTHON_PATH=C:\Python312\python.exe"
)
if not defined PYTHON_PATH (
  if exist "C:\Python311\python.exe" set "PYTHON_PATH=C:\Python311\python.exe"
)
if not defined PYTHON_PATH (
  if exist "C:\Python310\python.exe" set "PYTHON_PATH=C:\Python310\python.exe"
)

REM 3) Last resort: bare `python` on PATH, but skip the Windows Store stub.
if not defined PYTHON_PATH (
  where.exe python >nul 2>&1
  if %ERRORLEVEL%==0 (
    for /f "delims=" %%p in ('where.exe python') do (
      if not defined PYTHON_PATH (
        REM Skip Microsoft\WindowsApps stubs; they need an app-install from the Store.
        echo %%p | findstr /i "Microsoft\\WindowsApps" >nul
        if errorlevel 1 (
          set "PYTHON_PATH=%%p"
        ) else (
          echo [WARN] Ignoring Windows Store Python stub at %%p
        )
      )
    )
  )
)

if not defined PYTHON_PATH (
  echo [ERROR] Python 3.10+ was not found.
  echo Please install Python from https://www.python.org/downloads/
  echo and make sure "Add Python to PATH" is checked during installation.
  echo (On Windows, prefer the py -3 launcher over the Store stub.)
  pause
  exit /b 1
)

set "PYTHON_CMD=%PYTHON_PATH%"

if not exist "main.py" (
  echo [ERROR] main.py was not found next to this launcher.
  pause
  exit /b 1
)

echo [OK] Python : %PYTHON_CMD%
echo [OK] Starting WorkBuddy Manager...
echo.

"%PYTHON_CMD%" -u main.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ============================================
echo WorkBuddy Manager stopped with code %EXITCODE%.
echo If it stopped unexpectedly, keep this window open and send the error text.
echo ============================================
pause
exit /b %EXITCODE%
