@echo off
rem =====================================================================
rem  Write tools\secrets.cmd. Double-click this; type two passwords.
rem
rem  WHY THIS FILE EXISTS
rem  Because the instruction it replaces -- "download the file, then
rem  replace PASSWORD with your password" -- failed three times in a row,
rem  and not once because of anything the person did wrong:
rem
rem    1. the replacement put the whole connection URL in where only the
rem       password went, so the string contained itself. libpq reported
rem       that as `invalid integer value "postgresql:" for connection
rem       option "port"`, which names neither the file nor the mistake.
rem    2. the same replacement hit a second copy of the placeholder, in
rem       the guard that checked for it, so the guard then compared the
rem       value against itself and failed on a CORRECT password.
rem    3. the bare URL was pasted without `set "SOURCE="` around it, so
rem       cmd tried to RUN it: 'postgresql:' is not recognized.
rem
rem  Nothing here is replaced, pasted or quoted by hand. The passwords are
rem  typed at a masked prompt, never echoed, never put on a command line,
rem  and written straight into the file.
rem
rem  secrets.cmd is excluded by .gitignore and must never be committed.
rem =====================================================================

setlocal
set "SECRETS=%~dp0secrets.cmd"

echo.
echo   ============================================================
echo    Setting up tools\secrets.cmd
echo   ============================================================
echo.
echo   Two passwords. Neither is shown as you type, and neither is
echo   written anywhere except the file itself.
echo.

rem --- 1. the local database ---------------------------------------------
echo   [1 of 2] YOUR OWN PostgreSQL, the one pgAdmin connects to.
echo            User postgres, on localhost:5432, database marketdb.
echo.
set "LOCALUSER=postgres"
set /p "LOCALUSER=   user name [postgres]: "
if not defined LOCALUSER set "LOCALUSER=postgres"
set "LOCALDB=marketdb"
set /p "LOCALDB=   database  [marketdb]: "
if not defined LOCALDB set "LOCALDB=marketdb"

rem Masked, via PowerShell: cmd's set /p cannot hide what it reads.
for /f "delims=" %%P in ('powershell -NoProfile -Command "$p=Read-Host '   password ' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))"') do set "LOCALPASS=%%P"

if not defined LOCALPASS (
    echo.
    echo   No password typed. Nothing was written.
    echo.
    pause
    exit /b 1
)

rem --- 2. Render ----------------------------------------------------------
echo.
echo   [2 of 2] RENDER, from the dashboard: open the database, then
echo            "External Database URL". Press Enter to skip -- the task
echo            then imports from the published export instead, which
echo            needs no Render credentials and keeps working after
echo            Render deletes the free database at 30 days.
echo.
set "RENDERHOST="
set /p "RENDERHOST=   host (dpg-....frankfurt-postgres.render.com): "

set "RENDERUSER="
set "RENDERDB="
set "RENDERPASS="
if defined RENDERHOST (
    set /p "RENDERUSER=   user name: "
    set /p "RENDERDB=   database : "
    for /f "delims=" %%P in ('powershell -NoProfile -Command "$p=Read-Host '   password ' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))"') do set "RENDERPASS=%%P"
)

rem --- write it -----------------------------------------------------------
rem Delayed expansion is NOT used, deliberately: a password containing '!'
rem would be eaten by it. These are plain %VAR% reads outside any block.
> "%SECRETS%" echo @rem Written by set_secrets.cmd. Never commit this file.
>>"%SECRETS%" echo set "TARGET=postgresql://%LOCALUSER%:%LOCALPASS%@localhost:5432/%LOCALDB%"
if defined RENDERHOST (
    >>"%SECRETS%" echo set "SOURCE=postgresql://%RENDERUSER%:%RENDERPASS%@%RENDERHOST%/%RENDERDB%"
)

echo.
echo   Written: %SECRETS%
echo.

rem Shown WITHOUT the passwords, so the result can be checked at a glance
rem without putting the secret on screen -- which is how it reached a log
rem file the first time.
echo   It now contains, with the passwords hidden:
echo     set "TARGET=postgresql://%LOCALUSER%:********@localhost:5432/%LOCALDB%"
if defined RENDERHOST echo     set "SOURCE=postgresql://%RENDERUSER%:********@%RENDERHOST%/%RENDERDB%"
if not defined RENDERHOST echo     ^(no SOURCE line: imports come from the published export^)
echo.

rem A '%' in a password is the one character cmd still eats, and it has to
rem be doubled in the file. Checked rather than explained, because nobody
rem reads a warning about a character they do not have.
echo %LOCALPASS%%RENDERPASS% | findstr /C:"%%" >nul
if not errorlevel 1 (
    echo   ---------------------------------------------------------
    echo    One of the passwords contains a %% sign. cmd eats those in
    echo    pairs, so open tools\secrets.cmd and double it: a password
    echo    pa%%ss must be written pa%%%%ss. This is the only character
    echo    that needs it.
    echo   ---------------------------------------------------------
    echo.
)

echo   Now test it:   tools\auto_pull.cmd
echo   Then schedule: tools\install_auto_pull.cmd
echo.
pause
