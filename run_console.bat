@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"
set "XIUSHENLU_PROJECT_ROOT=%~dp0"
set "CONSOLE_PORT=8765"
set "LOCAL_ONLY=0"

if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage
if /I "%~1"=="--local-only" set "LOCAL_ONLY=1"
if /I "%~1"=="/local-only" set "LOCAL_ONLY=1"

if not "%~1"=="" if "%LOCAL_ONLY%"=="0" (
    echo Unknown argument: %~1
    echo.
    goto usage
)

if "%LOCAL_ONLY%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command ^
      "$projectRoot = $env:XIUSHENLU_PROJECT_ROOT;" ^
      "$url = 'http://127.0.0.1:' + $env:CONSOLE_PORT + '/';" ^
      "Write-Host ('Local-only URL: ' + $url);" ^
      "& (Join-Path $projectRoot 'run_console.ps1') -HostAddress '127.0.0.1' -Port ([int]$env:CONSOLE_PORT) -Url $url"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$projectRoot = $env:XIUSHENLU_PROJECT_ROOT;" ^
  "$ip = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | Where-Object { $_.OperationalStatus -eq 'Up' -and $_.NetworkInterfaceType -ne 'Loopback' } | ForEach-Object { $_.GetIPProperties().UnicastAddresses } | Where-Object { $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $_.Address.ToString() -notlike '127.*' -and $_.Address.ToString() -notlike '169.254.*' } | Select-Object -First 1 -ExpandProperty Address;" ^
  "if (-not $ip) { Write-Host 'No LAN IPv4 address was found. Make sure this computer is connected to Wi-Fi or LAN.'; exit 1 }" ^
  "$url = 'http://' + $ip.ToString() + ':' + $env:CONSOLE_PORT + '/';" ^
  "Write-Host ('Computer and mobile URL: ' + $url);" ^
  "Write-Host 'If Windows Firewall asks, allow Private networks.';" ^
  "& (Join-Path $projectRoot 'run_console.ps1') -HostAddress '0.0.0.0' -Port ([int]$env:CONSOLE_PORT) -Url $url"
exit /b

:usage
echo Usage:
echo   run_console.bat
echo   run_console.bat --local-only
echo.
echo Default mode listens on 0.0.0.0 so this computer and phones on the same Wi-Fi can access it.
echo --local-only listens on 127.0.0.1 for this computer only.
exit /b 0
