@echo off
rem =====================================================================
rem  Keep your own PostgreSQL current, on Windows.
rem
rem  Double-click it, or run it from a terminal. It stays open and keeps
rem  copying until you close the window.
rem
rem  SET THE TWO URLs BELOW FIRST. Nothing else needs changing.
rem
rem  Why a loop around the loop: replicate.py already survives a failed
rem  pass and retries. This restarts it if the PROCESS dies -- Windows
rem  sleeping, Python being upgraded underneath it, the console being
rem  killed. A copy that quietly stopped three days ago is worse than one
rem  that never started, because it still looks like it is working.
rem =====================================================================

setlocal

rem --- 1. YOUR database. The one the data should end up in. --------------
rem     If the password contains @ : / or # it must be percent-encoded:
rem     @ is %%40, : is %%3A, / is %%2F, # is %%23
set LOCAL_DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/marketdb

rem --- 2. The Render database to copy FROM. -----------------------------
rem     Render dashboard -> your database -> "External Database URL".
rem     NOT the internal one: that hostname only resolves inside Render.
set SOURCE_DATABASE_URL=postgresql://USER:PASSWORD@HOST.frankfurt-postgres.render.com/market_db_aktiv

rem --- 3. Optional: keep the status page from spinning down. ------------
rem     Clear this if you do not want the ping.
set DASHBOARD_URL=https://market-pipeline-aktiv.onrender.com

rem --- seconds between passes -------------------------------------------
set INTERVAL=10

rem ----------------------------------------------------------------------
cd /d "%~dp0.."

echo %LOCAL_DATABASE_URL% | findstr /C:"PASSWORD@localhost" >nul
if not errorlevel 1 (
    echo.
    echo   Open this file in Notepad and set the two connection strings
    echo   at the top first. It still has the placeholders in it.
    echo.
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not on PATH. Install it from python.org and tick
    echo   "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)

python -c "import psycopg2" >nul 2>&1
if errorlevel 1 (
    echo   Installing psycopg2-binary...
    python -m pip install --quiet psycopg2-binary
)

echo.
echo   Copying every %INTERVAL%s. Close this window to stop.
echo.

:loop
python tools\replicate.py --loop %INTERVAL% --keep-awake "%DASHBOARD_URL%"
echo.
echo   [%date% %time%] replicate.py exited. Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
