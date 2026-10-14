@echo off
rem =====================================================================
rem  One import, logged. This is what the scheduled task runs.
rem
rem  It calls tools\import_export.py, which fetches the current export
rem  straight from GitHub and writes into your own PostgreSQL. Only rows
rem  you do not already have, so running it often costs nothing.
rem
rem  SET THE LINE BELOW, then run install_auto_pull.cmd once.
rem  You can also double-click this any time for an import right now.
rem =====================================================================

setlocal

rem --- YOUR database ----------------------------------------------------
rem If the password contains @ : / or # it must be percent-encoded:
rem   @ is %%40   : is %%3A   / is %%2F   # is %%23
set TARGET=postgresql://postgres:PASSWORD@localhost:5432/marketdb

rem ----------------------------------------------------------------------
rem Everything is relative to this file, so the task works no matter what
rem directory Windows starts it in -- which is not the one you are in now.
set HERE=%~dp0
set LOGFILE=%HERE%auto_pull.log

echo %TARGET% | findstr /C:":PASSWORD@" >nul
if not errorlevel 1 (
    echo.
    echo   Open this file in Notepad and set TARGET at the top first.
    echo   It still says PASSWORD.
    echo.
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not on PATH. Install it from python.org and tick
    echo   "Add python.exe to PATH" in the installer.
    echo.
    pause
    exit /b 1
)

rem psycopg2 talks to PostgreSQL; tzdata is the IANA time-zone database,
rem which Windows does not ship -- without it the tools cannot resolve
rem Europe/Berlin. Both are no-ops once installed.
python -c "import psycopg2" >nul 2>&1 || python -m pip install --quiet psycopg2-binary
python -c "import tzdata"  >nul 2>&1 || python -m pip install --quiet tzdata

echo [%date% %time%] importing...>>"%LOGFILE%"
python "%HERE%import_export.py" --target "%TARGET%" >>"%LOGFILE%" 2>&1

if errorlevel 1 (
    echo [%date% %time%] FAILED - see the lines above.>>"%LOGFILE%"
    exit /b 1
)
echo [%date% %time%] ok>>"%LOGFILE%"
exit /b 0
