@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

set "ENV_NAME=xiushenlu"
set "ACTIVATED="

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
    call "%~dp0run_console.bat"
    exit /b
) else (
    python app\main.py %*
)

exit /b %ERRORLEVEL%
