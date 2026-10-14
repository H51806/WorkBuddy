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

set "PYTHON_CMD=C:\Users\feng\AppData\Local\Programs\Python\Python315\python.exe"
if not exist "%PYTHON_CMD%" (
  set "PYTHON_CMD=C:\Users\feng\AppData\Local\Programs\Python\Python312\python.exe"
)
if not exist "%PYTHON_CMD%" (
  set "PYTHON_CMD=C:\Python315\python.exe"
)
if not exist "%PYTHON_CMD%" (
  set "PYTHON_CMD=C:\Python312\python.exe"
)
if not exist "%PYTHON_CMD%" (
  set "PYTHON_CMD=python"
)

if not exist "main.py" (
  echo [ERROR] main.py was not found next to this launcher.
  pause
  exit /b 1
)

echo [OK] Python: %PYTHON_CMD%
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
