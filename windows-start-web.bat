@echo off
REM Double-click to run MooTrader on Windows.
REM
REM   OpenD (must already be running) -> web panel (detached) -> browser
REM
REM The trading scheduler is a SEPARATE process: press the play button in the
REM panel to start it. To stop everything, run windows-stop-web.bat.
REM
REM Unlike the Mac launcher this does NOT start OpenD for you. moomoo ships it
REM as a desktop app that keeps its own login session, and there is no reliable
REM way to launch and sign into it unattended -- so start OpenD yourself, log
REM in, and then run this.

setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

set "PY=.venv\Scripts\python.exe"
set "PORT=8770"
if not "%WEB_PORT%"=="" set "PORT=%WEB_PORT%"

echo.
echo   MooTrader
echo   ---------

if not exist "%PY%" (
    echo   x no interpreter at %PY%
    echo     run:  py -3.11 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
if not exist ".env" (
    echo   x no .env -- copy .env.example to .env and fill it in
    echo.
    pause
    exit /b 1
)

REM OpenD has to be up first. The bot talks to it on 127.0.0.1:11111 and there
REM is no way around that step, so say so plainly rather than starting a panel
REM that cannot reach a broker.
powershell -NoProfile -Command ^
  "if ((Test-NetConnection -ComputerName 127.0.0.1 -Port 11111 -InformationLevel Quiet)) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo   x OpenD is not listening on 127.0.0.1:11111
    echo     Start moomoo OpenD, finish the login, then run this again.
    echo.
    pause
    exit /b 1
)
echo   - OpenD ready on 127.0.0.1:11111

REM Stop a panel from an earlier run. Flask does not reload changed code, so
REM reusing one would serve whatever was current when it started.
if exist logs\web.pid (
    for /f "usebackq delims=" %%p in ("logs\web.pid") do taskkill /PID %%p /T /F >nul 2>&1
    del /q logs\web.pid >nul 2>&1
)

echo   - starting the panel...
REM Detached, so closing this window leaves the panel running -- the same
REM behaviour as the Mac launcher. windows-stop-web.bat is how you stop it.
powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath '%PY%' -ArgumentList 'web\server.py' -WindowStyle Hidden -PassThru -RedirectStandardOutput 'logs\web.out.log' -RedirectStandardError 'logs\web.err.log'; $p.Id | Out-File -Encoding ascii -NoNewline 'logs\web.pid'"

REM Wait for it to ANSWER, not just to exist: a pid about to die of a port
REM conflict or a bad .env looks healthy for the first second, and the imports
REM take several seconds cold.
set "UP="
for /l %%i in (1,1,40) do (
    if not defined UP (
        powershell -NoProfile -Command ^
          "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/favicon.ico' ^| Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
        if not errorlevel 1 set "UP=1"
        if not defined UP powershell -NoProfile -Command "Start-Sleep -Milliseconds 500" >nul
    )
)

if defined UP (
    echo   - panel on http://127.0.0.1:%PORT%
    start "" "http://127.0.0.1:%PORT%"
    timeout /t 2 >nul
    exit /b 0
)

echo   x the panel did not come up within 20s
echo.
echo   last lines of logs\web.err.log:
powershell -NoProfile -Command "if (Test-Path 'logs\web.err.log') { Get-Content 'logs\web.err.log' -Tail 15 }"
del /q logs\web.pid >nul 2>&1
echo.
pause
