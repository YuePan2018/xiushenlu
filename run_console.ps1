param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Url = "",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $Url) {
    $Url = "http://${HostAddress}:${Port}"
}

$LogDir = Join-Path $ProjectRoot "workspace"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$OutLog = Join-Path $LogDir "console.out.log"
$ErrLog = Join-Path $LogDir "console.err.log"

$script:ServerPid = $null
$script:CanStopServer = $false
$RestartCommand = -join ([char[]](0x91CD, 0x542F))

function Resolve-ConsolePython {
    if ($PythonPath) {
        return $PythonPath
    }

    if ($env:CONDA_PREFIX -and (Split-Path -Leaf $env:CONDA_PREFIX) -eq "xiushenlu") {
        $activeEnvPython = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $activeEnvPython) {
            return $activeEnvPython
        }
    }

    $candidateEnvRoots = @(
        (Join-Path $env:USERPROFILE ".conda\envs\xiushenlu"),
        (Join-Path $env:USERPROFILE "miniconda3\envs\xiushenlu"),
        (Join-Path $env:USERPROFILE "anaconda3\envs\xiushenlu"),
        (Join-Path $env:ProgramData "miniconda3\envs\xiushenlu"),
        (Join-Path $env:ProgramData "anaconda3\envs\xiushenlu")
    )
    foreach ($envRoot in $candidateEnvRoots) {
        $envPython = Join-Path $envRoot "python.exe"
        if (Test-Path $envPython) {
            return $envPython
        }
    }

    return "python"
}

function Get-ListeningConsolePid {
    $escapedHost = [regex]::Escape($HostAddress)
    $pattern = '^\s*TCP\s+{0}:{1}\s+\S+\s+LISTENING\s+(\d+)\s*$' -f $escapedHost, $Port
    $match = netstat -ano | Select-String -Pattern $pattern | Select-Object -First 1
    if (-not $match) {
        return $null
    }
    return [int]$match.Matches[0].Groups[1].Value
}

function Test-CanStopProcess {
    param([int]$ProcessId)

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        return $false
    }

    return $process.ProcessName -in @("python", "pythonw", "conda")
}

function Open-ConsolePage {
    Start-Process $Url
}

function Wait-ForConsolePort {
    for ($i = 0; $i -lt 40; $i++) {
        $listenPid = Get-ListeningConsolePid
        if ($listenPid) {
            $script:ServerPid = $listenPid
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Stop-ConsoleServer {
    param([switch]$Quiet)

    $targetPid = $script:ServerPid
    if (-not $targetPid) {
        $targetPid = Get-ListeningConsolePid
    }
    if (-not $targetPid) {
        return
    }
    if (-not $script:CanStopServer -and -not (Test-CanStopProcess -ProcessId $targetPid)) {
        if (-not $Quiet) {
            Write-Host "Port $Port is used by a non-Python process; it was not stopped. PID: $targetPid"
        }
        return
    }

    if (-not $Quiet) {
        Write-Host "Stopping Xiushenlu console. PID: $targetPid"
    }
    try {
        Stop-Process -Id $targetPid -ErrorAction Stop
    } catch {
        if (-not $Quiet) {
            Write-Host "Failed to stop console: $($_.Exception.Message)"
        }
    }

    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-ListeningConsolePid)) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    $script:ServerPid = $null
    $script:CanStopServer = $false
}

function Start-ConsoleServer {
    $existingPid = Get-ListeningConsolePid
    if ($existingPid) {
        $script:ServerPid = $existingPid
        $script:CanStopServer = Test-CanStopProcess -ProcessId $existingPid
        Write-Host "Xiushenlu console is already running: $Url (PID $existingPid)"
        Open-ConsolePage
        return
    }

    $consolePython = Resolve-ConsolePython
    Write-Host "Starting Xiushenlu console: $Url"
    Write-Host "Using Python: $consolePython"
    $process = Start-Process `
        -FilePath $consolePython `
        -ArgumentList @("app\main.py", "console", "--host", $HostAddress, "--port", "$Port") `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru

    $script:ServerPid = $process.Id
    $script:CanStopServer = $true

    if (Wait-ForConsolePort) {
        Write-Host "Xiushenlu console started: $Url (PID $script:ServerPid)"
        Open-ConsolePage
        return
    }

    Write-Host "Console did not start listening on port $Port in time."
    Write-Host "Logs:"
    Write-Host "  $OutLog"
    Write-Host "  $ErrLog"
}

function Restart-ConsoleServer {
    Stop-ConsoleServer
    Start-ConsoleServer
}

try {
    Start-ConsoleServer
    Write-Host ""
    Write-Host ("Type '{0}' to restart the console and open the page again." -f $RestartCommand)
    Write-Host "Press Ctrl+C to stop the console and end this PowerShell session."

    while ($true) {
        $command = (Read-Host "Command").Trim()
        if ($command -eq $RestartCommand) {
            Restart-ConsoleServer
        } elseif ($command) {
            Write-Host ("Unknown command: {0}. Available command: {1}" -f $command, $RestartCommand)
        }
    }
} finally {
    Stop-ConsoleServer -Quiet
    [Environment]::Exit(0)
}
