@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py %*
) else (
  python server.py %*
)
if errorlevel 1 (
  echo.
  echo Python 3 is required. Install Python 3.10 or later, then rerun this file.
  pause
)