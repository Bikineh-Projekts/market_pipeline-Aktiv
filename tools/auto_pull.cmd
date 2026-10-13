@echo off
rem =====================================================================
rem  One pull, logged. This is what the scheduled task runs.
rem
rem  It does not take the Render password: that already lives inside your
rem  database, in the USER MAPPING that sql/06_pull_from_render.sql
rem  created. This only connects to your OWN database on localhost and
rem  asks it to go and fetch.
rem
rem  Run install_auto_pull.cmd once to have Windows call this by itself.
rem  You can also double-click this any time for a pull right now.
rem =====================================================================

setlocal enabledelayedexpansion

rem --- YOUR local database -----------------------------------------------
rem Leave the password OUT of this line if you would rather not have one in
rem a file: create %%APPDATA%%\postgresql\pgpass.conf containing
rem     localhost:5432:*:postgres:YOUR_PASSWORD
rem and libpq reads it from there. Either way works.
set LOCAL_DB=postgresql://postgres:PASSWORD@localhost:5432/market_pipeline-Aktiv

rem ----------------------------------------------------------------------
set LOGFILE=%~dp0auto_pull.log

rem Find psql. PostgreSQL installs it even if you only ever use pgAdmin,
rem so there is nothing to install -- it just is not on PATH by default.
set PSQL=
if defined PGBIN if exist "%PGBIN%\psql.exe" set PSQL=%PGBIN%\psql.exe
if not defined PSQL (
    for /f "delims=" %%P in ('dir /b /ad /o-n "C:\Program Files\PostgreSQL" 2^>nul') do (
        if not defined PSQL if exist "C:\Program Files\PostgreSQL\%%P\bin\psql.exe" (
            set PSQL=C:\Program Files\PostgreSQL\%%P\bin\psql.exe
        )
    )
)
if not defined PSQL (
    for %%P in (psql.exe) do if not defined PSQL if exist "%%~$PATH:P" set PSQL=%%~$PATH:P
)

if not defined PSQL (
    echo [%date% %time%] psql.exe not found. Set PGBIN to the folder that holds it.>>"%LOGFILE%"
    echo psql.exe not found.
    echo Looked in C:\Program Files\PostgreSQL\*\bin and on PATH.
    echo Set PGBIN to the folder containing psql.exe and try again.
    exit /b 1
)

echo %LOCAL_DB% | findstr /C:":PASSWORD@" >nul
if not errorlevel 1 (
    echo Edit LOCAL_DB at the top of this file first — it still says PASSWORD.
    echo [%date% %time%] not configured: LOCAL_DB still has the placeholder.>>"%LOGFILE%"
    exit /b 1
)

rem ON_ERROR_STOP so a failure is a non-zero exit code rather than a
rem cheerful log line. Task Scheduler shows the exit code in its history,
rem which is the only place anyone looks when it has quietly stopped.
echo [%date% %time%] pulling...>>"%LOGFILE%"
"%PSQL%" "%LOCAL_DB%" -v ON_ERROR_STOP=1 -At ^
    -c "SELECT table_name || ': +' || rows_added || ' new, ' || rows_updated || ' updated' FROM pull_from_render() WHERE rows_added > 0 OR rows_updated > 0;" >>"%LOGFILE%" 2>&1

if errorlevel 1 (
    echo [%date% %time%] FAILED — see the lines above.>>"%LOGFILE%"
    exit /b 1
)
echo [%date% %time%] ok>>"%LOGFILE%"
exit /b 0
