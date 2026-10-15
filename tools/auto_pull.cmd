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
rem
rem TWO CHARACTERS BATCH ITSELF EATS, whatever the URL rules say:
rem
rem   %  must be written twice.  A percent-encoded password like
rem      p%%40ss arrives as p@ss only if you type p%%%%40ss here --
rem      cmd consumes one %% of every pair before the URL is ever seen.
rem
rem   &  ends a command.  It is why every SET below is written
rem      set "NAME=value" with the quotes around the WHOLE assignment:
rem      unquoted, a connection string ending ?sslmode=require&channel...
rem      would set NAME, then try to run `channel...` as a program.
set "TARGET=postgresql://postgres:PASSWORD@localhost:5432/marketdb"

rem --- The RENDER database, if you want the two connected DIRECTLY -------
rem Render dashboard -> your Postgres -> Connections -> EXTERNAL Database
rem URL. Paste it here and this task copies Render straight into your
rem database, with no file and no GitHub in between -- which is what you
rem actually asked for.
rem
rem Leave it EMPTY and the task falls back to the published export, which
rem needs no Render credentials and keeps working after Render deletes the
rem database at 30 days. Both end up with the same rows; the direct one is
rem fresher, because it does not wait for an export to be built.
rem
rem *** DO NOT PUT THE RENDER URL ON THE LINE BELOW. ***
rem This file is tracked by git. A password typed here is a password
rem published the moment you commit -- and this repository is public, so
rem "I will delete it later" does not help: it stays in the history and
rem has to be rotated at Render instead.
rem
rem Put it in tools\secrets.cmd, which .gitignore excludes and which this
rem reads a few lines down. One line, no quotes needed around the whole
rem assignment because the file is not parsed as a command line:
rem
rem     set "SOURCE=postgresql://user:password@host.../market_db_aktiv"
rem
set "SOURCE="

rem Untracked, so the credential never reaches git. Missing is fine: the
rem task falls back to the published export.
if exist "%~dp0secrets.cmd" call "%~dp0secrets.cmd"

rem ----------------------------------------------------------------------
rem Everything is relative to this file, so the task works no matter what
rem directory Windows starts it in -- which is not the one you are in now.
set "HERE=%~dp0"
set "LOGFILE=%HERE%auto_pull.log"

rem Pure string substitution, no pipe and no errorlevel. The previous
rem version was `echo "%TARGET%" | findstr ... ` and read the errorlevel
rem afterwards -- which a failed `call secrets.cmd` a few lines above can
rem leave set, so the guard fired on a TARGET that was perfectly correct
rem and sent the user to edit a line that needed no editing.
rem
rem If removing the text changes the string, the string contained it.
if not "%TARGET%"=="%TARGET:PASSWORD@=%" (
    echo.
    echo   TARGET at the top of this file still says PASSWORD.
    echo   Open tools\auto_pull.cmd in Notepad and put your real local
    echo   password in it.
    echo.
    pause
    exit /b 1
)

rem secrets.cmd is the usual way this goes wrong: pasting the bare URL
rem instead of the whole `set "SOURCE=..."` line makes cmd try to RUN the
rem URL, which reads as 'postgresql:' is not recognized -- a message that
rem names neither the file nor what to do about it.
if defined SOURCE if not "%SOURCE:~0,11%"=="postgresql:" if not "%SOURCE:~0,9%"=="postgres:" (
    echo.
    echo   SOURCE does not look like a connection string:
    echo     %SOURCE%
    echo.
    echo   tools\secrets.cmd must contain the WHOLE line, set and quotes:
    echo     set "SOURCE=postgresql://user:password@host/dbname"
    echo   not just the URL on its own.
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
