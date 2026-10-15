@echo off
rem =====================================================================
rem  Make the pull automatic. Double-click once; Windows does the rest.
rem
rem  This registers a Windows scheduled task that runs auto_pull.cmd every
rem  N minutes. Nothing to install: Task Scheduler is part of Windows, and
rem  the script installs psycopg2 and tzdata itself if they are missing.
rem
rem  It imports from the export GitHub publishes, through
rem  tools\import_export.py. Only rows your database does not already
rem  have, so a run with nothing new costs a second.
rem
rem  WHY NOT LOGICAL REPLICATION, WHICH WOULD BE AUTOMATIC BY ITSELF
rem  Because Render will not do it on this plan. Render's own docs: logical
rem  replication is off by default, the workspace must be on Pro or higher,
rem  the database needs at least 10 GB, and Render support has to switch it
rem  on. CREATE PUBLICATION on a free database cannot succeed, which is
rem  exactly what you found.
rem
rem  WHY NOT pg_cron
rem  It does not run on Windows.
rem
rem  To stop it later:     schtasks /Delete /TN "MarketPipeline Pull" /F
rem  To see what it did:   tools\auto_pull.log
rem =====================================================================

setlocal

rem --- how often, in minutes --------------------------------------------
rem The export is rebuilt when the ETL finishes, every 30 minutes at
rem best, and the free API quota caps it there -- so anything under
rem about 10 minutes asks the same question more often than the answer
rem can possibly change.
set EVERY_MINUTES=15

set TASKNAME=MarketPipeline Pull
set SCRIPT=%~dp0auto_pull.cmd

if not exist "%SCRIPT%" (
    echo Cannot find auto_pull.cmd next to this file.
    echo Keep both in the same folder.
    pause
    exit /b 1
)

rem No placeholder pre-check here. This runs auto_pull.cmd below and
rem refuses to schedule anything if it exits non-zero, so auto_pull's
rem own guard is the single authority -- a second copy of the same
rem rule in a different dialect was only a second way to be wrong.

echo.
echo   Testing one pull before scheduling anything...
echo.
call "%SCRIPT%"
if errorlevel 1 (
    echo.
    echo   That failed, so nothing was scheduled. See tools\auto_pull.log.
    echo   A task that fails every 15 minutes forever is worse than no task.
    echo.
    pause
    exit /b 1
)

echo   The pull works. Scheduling it every %EVERY_MINUTES% minutes.
echo.

rem /F replaces an existing task of the same name, so running this twice
rem updates the schedule instead of failing or creating a second copy.
schtasks /Create /TN "%TASKNAME%" /TR "\"%SCRIPT%\"" /SC MINUTE /MO %EVERY_MINUTES% /F
if errorlevel 1 (
    echo.
    echo   Could not create the task. Right-click this file and choose
    echo   "Run as administrator", then try again.
    echo.
    pause
    exit /b 1
)

echo.
echo   Done. Your database now updates itself every %EVERY_MINUTES% minutes.
echo.
echo     check it ran:   type "%~dp0auto_pull.log"
echo     check in SQL:   SELECT * FROM render_sync_status;
echo     stop it:        schtasks /Delete /TN "%TASKNAME%" /F
echo.
echo   One caveat worth knowing: this runs while you are logged in. It
echo   does not run when the computer is off, and it does not need to -
echo   the pull only ever copies what it has not seen, so a machine that
echo   was off for a week catches up on the next run.
echo.
pause
