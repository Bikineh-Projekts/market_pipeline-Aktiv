@echo off
rem =====================================================================
rem  Is the local copy sound? Double-click to find out.
rem
rem  Runs sql\07_healthcheck.sql against your database and prints one
rem  line per check. Writes nothing, locks nothing -- safe at any time,
rem  including while a pull is running.
rem
rem  The connection comes from tools\secrets.cmd, so nothing to type.
rem =====================================================================

setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not on PATH.
    echo.
    pause
    exit /b 1
)

python "%~dp0healthcheck.py"
set RC=%ERRORLEVEL%

echo.
pause
exit /b %RC%
