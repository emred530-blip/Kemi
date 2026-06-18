@echo off
REM Kemi - double-click this file to start (Windows).
cd /d "%~dp0\.."
echo Starting Kemi... a browser window will open in a moment.
where kemi >nul 2>nul
if %errorlevel%==0 (
    kemi app
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    python -m kemi app
    goto :eof
)
echo Python 3.10+ is required. Install it from https://www.python.org/downloads/ and try again.
pause
