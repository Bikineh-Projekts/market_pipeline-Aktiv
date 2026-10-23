@echo off
rem =====================================================================
rem  Merge the duplicate interval codes in BOTH databases. One click.
rem
rem  Alpha Vantage calls the daily interval "daily", Twelve Data calls it
rem  "1day", and both strings reached dim_interval -- so daily candles
rem  and daily indicators ended up under two different interval_ids. No
rem  error was ever raised; a Power BI join on interval just returned
rem  nothing.
rem
rem  Safe to run twice. A database that is already clean is left alone.
rem =====================================================================

setlocal
where python >nul 2>&1 || (echo.& echo   Python is not on PATH.& echo.& pause & exit /b 1)
python "%~dp0migrate_intervals.py"
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
