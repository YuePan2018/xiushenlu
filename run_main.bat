@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

set "ENV_NAME=xiushenlu"
set "CONSOLE_HOST=127.0.0.1"
set "CONSOLE_PORT=8765"
set "CONSOLE_URL=http://%CONSOLE_HOST%:%CONSOLE_PORT%"
set "ACTIVATED="
set "EXISTING_PID="

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

call conda activate "%ENV_NAME%" >nul 2>&1
if not errorlevel 1 set "ACTIVATED=1"

if not defined ACTIVATED if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" "%ENV_NAME%"
    if not errorlevel 1 set "ACTIVATED=1"
)

if not defined ACTIVATED if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\anaconda3\Scripts\activate.bat" "%ENV_NAME%"
    if not errorlevel 1 set "ACTIVATED=1"
)

if not defined ACTIVATED if exist "%ProgramData%\miniconda3\Scripts\activate.bat" (
    call "%ProgramData%\miniconda3\Scripts\activate.bat" "%ENV_NAME%"
    if not errorlevel 1 set "ACTIVATED=1"
)

if not defined ACTIVATED if exist "%ProgramData%\anaconda3\Scripts\activate.bat" (
    call "%ProgramData%\anaconda3\Scripts\activate.bat" "%ENV_NAME%"
    if not errorlevel 1 set "ACTIVATED=1"
)

if not defined ACTIVATED (
    echo Failed to activate conda environment: %ENV_NAME%
    echo Please make sure conda is installed and the environment exists.
    pause
    exit /b 1
)

if "%~1"=="" (
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr /C:"%CONSOLE_HOST%:%CONSOLE_PORT%" ^| findstr /C:"LISTENING"') do set "EXISTING_PID=%%P"

    if defined EXISTING_PID (
        echo Xiushenlu console is already running at %CONSOLE_URL% ^(PID %EXISTING_PID%^).
        start "" "%CONSOLE_URL%"
        exit /b 0
    )

    echo Starting Xiushenlu console at %CONSOLE_URL%
    echo Close this window or press Ctrl+C to stop the console.
    echo.
    start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%CONSOLE_URL%'"
    python app\main.py console --host "%CONSOLE_HOST%" --port "%CONSOLE_PORT%"
    echo.
    pause
) else (
    python app\main.py %*
)

exit /b %ERRORLEVEL%
