@echo off
rem =====================================================================
rem  Write tools\secrets.cmd. Double-click this; nothing has to be typed.
rem
rem  The work is in set_secrets.py next door, because the three things
rem  this has to get right are all things batch is bad at: reading the
rem  clipboard, pulling a connection URL apart, and doubling a '%' so cmd
rem  does not eat it.
rem
rem  WHY IT EXISTS AT ALL
rem  "Download the file, then replace PASSWORD with your password" failed
rem  three times, never because of anything done wrong:
rem
rem    1. the replacement put the whole connection URL in where only the
rem       password went, so the string contained itself. libpq reported
rem       that as `invalid integer value "postgresql:" for connection
rem       option "port"` -- a port nobody typed, no mention of the file.
rem    2. the same replacement also hit the placeholder inside the guard
rem       that checked for it, so the guard compared the value against
rem       itself and failed on a CORRECT password.
rem    3. the bare URL was pasted without `set "SOURCE="` around it, so
rem       cmd tried to run it: 'postgresql:' is not recognized.
rem
rem  So there is no longer anything to replace, and no longer any need to
rem  split the Render URL into parts by hand -- paste it whole and the
rem  pieces are taken out of it.
rem
rem  secrets.cmd is excluded by .gitignore and must never be committed.
rem =====================================================================

setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not on PATH. Install it from python.org and tick
    echo   "Add python.exe to PATH" in the installer.
    echo.
    pause
    exit /b 1
)

python -c "import psycopg2" >nul 2>&1 || python -m pip install --quiet psycopg2-binary

python "%~dp0set_secrets.py"
set RC=%ERRORLEVEL%

echo.
pause
exit /b %RC%
