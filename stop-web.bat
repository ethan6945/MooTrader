@echo off
REM Double-click to stop MooTrader completely.
REM
REM Order matters: the trading worker goes first, through the start protocol, so
REM it closes its session and releases its lease together. Killing it outright
REM leaves a session with no ended_at and a lease naming a dead pid, which the
REM next start has to break before it can proceed. Stopping the web server first
REM would remove the thing that knows how to stop the worker properly.

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PORT=8770"
if not "%WEB_PORT%"=="" set "PORT=%WEB_PORT%"

echo.
echo   MooTrader -- stopping everything
echo   -------------------------------

if exist "%PY%" (
    echo   - stopping the trading worker...
    set "MMT_HOME=%CD%"
    set "PYTHONPATH=%CD%"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from src import start_protocol as s; r=s.stop('stop-bat'); print('     worker:', 'stopped' if r.get('stopped') else 'was not running')"
)

echo   - leaving OpenD running ^(start it and stop it yourself^)

if exist logs\web.pid (
    for /f "usebackq delims=" %%p in ("logs\web.pid") do (
        taskkill /PID %%p /T /F >nul 2>&1
        echo   - web server stopped ^(pid %%p^)
    )
    del /q logs\web.pid >nul 2>&1
) else (
    echo   - web server: no pid file
)

REM Anything still holding the port that we did not start. Named, not killed
REM silently -- it may not be ours.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr ":%PORT% "') do (
    echo   ! pid %%p still holds port %PORT%
    powershell -NoProfile -Command "Get-Process -Id %%p -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName"
)

echo.
timeout /t 2 >nul
