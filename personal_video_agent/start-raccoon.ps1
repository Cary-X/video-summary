param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$address = 'http://127.0.0.1:8507/'

function Test-RaccoonReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($address + '_stcore/health') -TimeoutSec 1
        return $response.StatusCode -eq 200 -and $response.Content.Trim() -eq 'ok'
    } catch {
        return $false
    }
}

function Update-RaccoonShortcut {
    $icon = Join-Path $PSScriptRoot 'assets\raccoon-launcher.ico'
    $target = Join-Path $PSScriptRoot 'Start Raccoon Agent.cmd'
    $shortcutPath = Join-Path $PSScriptRoot 'Start Raccoon Agent.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if ($shortcut.TargetPath -eq $target -and $shortcut.IconLocation -eq "$icon,0") {
        return
    }
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.IconLocation = "$icon,0"
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Open Raccoon Agent'
    $shortcut.Save()
}

try {
    if (-not (Test-RaccoonReady)) {
        $python = Join-Path $PSScriptRoot '.venv64\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python)) {
            throw 'Python environment .venv64 is missing. Follow the setup steps in README.md.'
        }
        $dataDir = Join-Path $PSScriptRoot 'data'
        New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
        $errorLog = Join-Path $dataDir 'launcher.stderr.log'
        $worker = Start-Process -FilePath $python -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
            -ArgumentList '-m streamlit run app.py --server.address 127.0.0.1 --server.port 8507 --server.headless true' `
            -RedirectStandardOutput (Join-Path $dataDir 'launcher.stdout.log') -RedirectStandardError $errorLog
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-RaccoonReady)) {
            if ($worker.HasExited -or [DateTime]::UtcNow -ge $deadline) {
                throw "Raccoon Agent did not start. Check $errorLog"
            }
            Start-Sleep -Milliseconds 300
        }
    }
    try {
        Update-RaccoonShortcut
    } catch {
        Write-Warning "Could not update the folder shortcut. $($_.Exception.Message)"
    }
    if (-not $NoBrowser) {
        Start-Process $address
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
