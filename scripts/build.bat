@echo off
setlocal
cd /d "%~dp0.."
cd src
set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found in PATH.
    echo Please install Python 3.10 plus first.
    echo Tick the option to add python.exe to PATH during install.
    echo Or use the py launcher from a normal Command Prompt.
    echo You can also build manually with PyInstaller using src\USB2Net.spec
    echo.
    echo Press any key to close...
    pause >nul
    exit /b 1
)
if not exist "..\.venv" (
    echo [setup] Creating virtual environment named .venv
    %PY% -m venv "..\.venv"
)
if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] Failed to create .venv. Run as Administrator or reinstall Python.
    echo.
    echo Press any key to close...
    pause >nul
    exit /b 1
)
set "VENV_PY=..\.venv\Scripts\python.exe"
echo [setup] Installing build dependencies now
%VENV_PY% -m pip install --upgrade pip
%VENV_PY% -m pip install -r requirements.txt
%VENV_PY% -m pip install pyinstaller
echo [build] Running PyInstaller
%VENV_PY% -m PyInstaller --noconfirm --clean --distpath ..\dist --workpath ..\build USB2Net.spec
if errorlevel 1 (
    echo [ERROR] Build failed. See the output above.
    echo.
    echo Press any key to close...
    pause >nul
    exit /b 1
)
echo.
echo Build finished. Output file is dist\USB2Net.exe
echo.
echo Press any key to close...
pause >nul
endlocal
