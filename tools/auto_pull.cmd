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

rem --- WHERE THE CONNECTION STRINGS COME FROM ---------------------------
rem Both of them live in tools\secrets.cmd, which .gitignore excludes.
rem NOTHING IN THIS FILE NEEDS EDITING, EVER. Re-download it any time.
rem
rem DO NOT WRITE secrets.cmd BY HAND. Run tools\set_secrets.cmd: it asks
rem for the passwords at a masked prompt and writes the file itself. Every
rem failure this setup has had came from editing it by hand -- a whole URL
rem pasted in where only the password went, a placeholder replaced in two
rem places at once, the bare URL without the `set "..."` around it.
rem
rem For reference, it is two lines:
rem
rem   set "TARGET=postgresql://postgres:PASS@localhost:5432/marketdb"
rem   set "SOURCE=postgresql://user:PASS@host.../market_db_aktiv"
rem
rem TARGET is your own database and is required.
rem SOURCE is Render. Leave the line out entirely and the task falls back
rem to the published export, which needs no Render credentials and keeps
rem working after Render deletes the database at 30 days.
rem
rem Two characters batch eats, whatever the URL rules say:
rem   %  write it twice. A password p%%40ss must be typed p%%%%40ss.
rem   &  ends a command, which is why both lines are set "NAME=value"
rem      with quotes around the WHOLE assignment.
rem
rem This file deliberately contains no placeholder password. The previous
rem version did, in two places -- the TARGET line and the guard that
rem checked it -- and a search-and-replace meant for the first silently
rem rewrote the second, so the guard compared TARGET against itself with
rem the real password removed and failed every run on a correct value.

set "TARGET="
set "SOURCE="
if exist "%~dp0secrets.cmd" call "%~dp0secrets.cmd"

if not defined TARGET (
    echo.
    echo   No database to write to.
    echo.
    echo   Run this, which writes it for you and asks for the password
    echo   at a prompt that does not show what you type:
    echo.
    echo     tools\set_secrets.cmd
    echo.
    echo   Writing tools\secrets.cmd by hand is where every previous
    echo   attempt went wrong, so there is no longer any need to.
    echo.
    pause
    exit /b 1
)

rem ----------------------------------------------------------------------
rem Everything is relative to this file, so the task works no matter what
rem directory Windows starts it in -- which is not the one you are in now.
set "HERE=%~dp0"
set "LOGFILE=%HERE%auto_pull.log"

rem secrets.cmd is the usual way this goes wrong: pasting the bare URL
rem instead of the whole `set "SOURCE=..."` line makes cmd try to RUN the
rem URL, which reads as 'postgresql:' is not recognized -- a message that
rem names neither the file nor what to do about it.
if defined SOURCE if not "%SOURCE:~0,11%"=="postgresql:" if not "%SOURCE:~0,9%"=="postgres:" (
    echo.
    echo   SOURCE does not look like a connection string:
    echo     %SOURCE%
    echo.
    echo   Rather than fixing it by hand, run this and type the password
    echo   at the prompt -- it writes the file correctly:
    echo.
    echo     tools\set_secrets.cmd
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

rem --- keep the dashboard awake -----------------------------------------
rem A free Render WEB SERVICE spins down after 15 minutes without traffic.
rem The database does NOT -- Render spins down web services only -- so this
rem changes nothing about whether data keeps flowing. It is about the
rem status page opening instantly instead of after a minute.
rem
rem There is a keepalive workflow in GitHub Actions meant to do this, and
rem it cannot: it asks for a ping every 14 minutes and GitHub delivered 4
rem in a day. Against a 15-minute spin-down that leaves the page asleep
rem essentially always. This task already runs every 15 minutes on a
rem machine that is actually yours, which is the right cadence by accident.
rem
rem The window is deliberate. The free plan allows 750 instance-hours a
rem month; awake around the clock is about 744 of them -- the whole
rem allowance, for a page nobody reads at 04:00. 08:00-22:59 is about 465.
rem Clear DASHBOARD to turn the ping off.
set "DASHBOARD=https://market-pipeline-aktiv.onrender.com"

rem Computed OUTSIDE any parenthesised block on purpose. cmd expands
rem %VAR% when it PARSES a block, not when it runs it, so setting HOUR and
rem reading it inside the same ( ) would read the previous run's value --
rem empty on the first run, which is a syntax error, not a wrong answer.
for /f "tokens=1 delims=:" %%H in ("%TIME: =0%") do set RAWHOUR=%%H
set /a HOUR=1%RAWHOUR%-100

if defined DASHBOARD if %HOUR% GEQ 8 if %HOUR% LEQ 22 curl -s -o nul -m 20 "%DASHBOARD%/health"

if defined SOURCE goto :direct

echo [%date% %time%] importing from the published export...>>"%LOGFILE%"
python "%HERE%import_export.py" --target "%TARGET%" >>"%LOGFILE%" 2>&1
goto :done

:direct
echo [%date% %time%] copying straight from Render...>>"%LOGFILE%"
python "%HERE%replicate.py" --source "%SOURCE%" --target "%TARGET%" --once >>"%LOGFILE%" 2>&1

:done

if errorlevel 1 (
    echo [%date% %time%] FAILED - see the lines above.>>"%LOGFILE%"
    exit /b 1
)
echo [%date% %time%] ok>>"%LOGFILE%"
exit /b 0
