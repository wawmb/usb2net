@echo off
setlocal
cd /d "%~dp0.."
cd src

title USB2Net

set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="

set "PY="
where python >nul 2>&1
if %errorlevel%==0 (
    set "PY=python"
    goto CHECK
)
where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py -3"
    goto CHECK
)
echo [ERROR] Python not found. Install Python 3.8+ and tick "Add to PATH".
echo Download: https://www.python.org/downloads/
echo.
echo Press any key to exit...
pause >nul
exit /b 1

:CHECK
echo [INFO] Using interpreter: %PY%
%PY% -c "import serial" >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] pyserial is ready.
) else (
    echo [INFO] pyserial missing, installing...
    %PY% -m pip install pyserial
    if %errorlevel% neq 0 (
        echo [ERROR] pyserial install failed.
        echo.
        echo Press any key to exit...
        pause >nul
        exit /b 1
    )
    echo [INFO] pyserial installed.
)

%PY% -c "import websockets" >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] websockets is ready.
) else (
    echo [INFO] websockets missing, installing...
    %PY% -m pip install websockets
    if %errorlevel% neq 0 (
        echo [ERROR] websockets install failed.
        echo.
        echo Press any key to exit...
        pause >nul
        exit /b 1
    )
    echo [INFO] websockets installed.
)

:LAUNCH
echo [INFO] Launching USB2Net...
%PY% main.py
set "EXITCODE=%errorlevel%"
if %EXITCODE% neq 0 (
    echo [ERROR] USB2Net exited with code %EXITCODE%
    echo Please send me the red error text above.
    echo.
    echo Press any key to exit...
    pause >nul
)
