@echo off
REM ============================================================
REM  Aurum FX / LumixTrade — Bridge watchdog wrapper (v1.8)
REM ============================================================
REM  Restarts the bridge automatically if it crashes.
REM  Exit codes from aurum_bridge.py:
REM    0  = clean shutdown (Ctrl+C)         -> DO NOT restart
REM    1  = MT5 initial connect failed      -> restart after 30s
REM    2  = missing env vars (config error) -> DO NOT restart
REM    3  = MT5 reconnect exhausted         -> restart after 15s
REM    other (crash) -> restart after 10s
REM ============================================================

REM ---- EDIT THESE for your VPS ----
set "AURUM_API_URL=https://lumixtrade.live/api"
set "AURUM_API_KEY=PUT-YOUR-BRIDGE-KEY-HERE"
set "MT5_LOGIN=PUT-YOUR-MT5-LOGIN"
set "MT5_PASSWORD=PUT-YOUR-MT5-PASSWORD"
set "MT5_SERVER=PUT-YOUR-MT5-SERVER"
REM Optional tuning
set "AURUM_POLL_INTERVAL=5"
set "AURUM_CANDLES_PUSH_INTERVAL=60"
set "AURUM_STREAM_CFG_INTERVAL=60"
set "AURUM_LOG_LEVEL=INFO"
REM ---------------------------------

cd /d "%~dp0"

:loop
echo.
echo [%date% %time%] starting aurum_bridge.py ...
python aurum_bridge.py
set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] bridge exited with code %EXITCODE%

if "%EXITCODE%"=="0" (
    echo [%date% %time%] clean shutdown - exiting watchdog.
    goto :eof
)
if "%EXITCODE%"=="2" (
    echo [%date% %time%] config error - missing env vars. Edit this .bat and retry.
    pause
    goto :eof
)
if "%EXITCODE%"=="3" (
    echo [%date% %time%] MT5 reconnect exhausted - restarting in 15s ...
    timeout /t 15 /nobreak >nul
    goto loop
)
if "%EXITCODE%"=="1" (
    echo [%date% %time%] MT5 initial connect failed - restarting in 30s ...
    timeout /t 30 /nobreak >nul
    goto loop
)

echo [%date% %time%] unexpected exit - restarting in 10s ...
timeout /t 10 /nobreak >nul
goto loop
