param([switch]$FromScheduledTask, [switch]$ShowStatus)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'Interactive desktop required; Session 0 refused.' }
$pythonw = Join-Path $PSScriptRoot '.venv-wechat\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Existing sender Python environment missing.' }
$trayScript = Join-Path $PSScriptRoot 'tray_app.py'
$launcherPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$launcherScript = Join-Path $PSScriptRoot 'tray_launcher.py'
if ($ShowStatus) { & $launcherPython -X utf8 $launcherScript --show-status }
else { & $launcherPython -X utf8 $launcherScript }
if ($LASTEXITCODE -ne 0) { throw 'Independent tray launch failed; use the project shortcut from Explorer.' }
. (Join-Path $PSScriptRoot 'common.ps1')
$trayDataRoot = $configRoot
$trayStatusFile = Join-Path $trayDataRoot 'tray-status.json'
$trayWaitDeadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
do {
Start-Sleep -Milliseconds 250
if (Test-Path -LiteralPath $trayStatusFile) {
    $trayStatus = Get-Content -LiteralPath $trayStatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $trayHeartbeat = [DateTimeOffset]::FromUnixTimeSeconds([long]$trayStatus.heartbeat)
    if ((([DateTimeOffset]::UtcNow - $trayHeartbeat).TotalSeconds -lt 15) -and (Get-Process -Id $trayStatus.pid -ErrorAction SilentlyContinue)) {
        Write-Output ('Tray running; single-instance PID=' + $trayStatus.pid + '; state=' + $trayStatus.status)
        exit 0
    }
}
} while ([DateTimeOffset]::UtcNow -lt $trayWaitDeadline)
throw 'Tray did not report a live status. See tray.log in the data directory selected by SMS_WECHAT_DATA_DIR.'
