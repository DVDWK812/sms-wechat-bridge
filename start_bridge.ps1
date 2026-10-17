param([switch]$FromScheduledTask)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$configFile = Join-Path $configRoot 'config.json'
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $config.SMS_BIND -or -not $config.SMS_ALLOWED_CLIENT_IPS) {
    throw 'Tailscale addresses are not configured; no listener started.'
}
if ($FromScheduledTask -and -not $config.AcceptanceComplete) {
    throw 'Autostart requires completed device and WeChat acceptance tests.'
}
if ($config.SMS_DRY_RUN -eq '0') {
    if ($config.SMS_REAL_SEND_APPROVED -ne '1' -or $config.WECHAT_TARGET_VERIFIED -ne '1') {
        throw 'Real sending has not been explicitly approved and target verified.'
    }
    if ($config.WECHAT_TARGET_TYPE -notin @('friend','group','chat')) { throw 'A verified friend or group is required.' }
    if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'Interactive desktop required; Session 0 refused.' }
    $wxPaths = @(Get-Process Weixin,WeChat -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique)
    if ($wxPaths.Count -ne 1) { throw 'Cannot determine a single running WeChat client.' }
    $wxVersion = [version](Get-Item -LiteralPath $wxPaths[0]).VersionInfo.FileVersion
    if ($wxVersion -ne [version]'4.1.15.13') {
        throw "Current WeChat $wxVersion requires a fresh OCR sender acceptance test."
    }
    $pythonProof = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    # Share the tray's strict target/version identity gate. Identity-only
    # navigation is recorded separately from historical actual-send evidence.
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $pythonProof -X utf8 -c 'from tray_control import ROOT, read_json, friend_proof; import sys; sys.exit(0 if friend_proof(ROOT, read_json(sys.argv[1])) else 1)' $configFile
        if ($LASTEXITCODE -ne 0) { throw 'Chat identity proof does not match this target/client.' }
    } finally { Pop-Location }
}
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Project Python 3.12 venv is missing.' }
$secureSecret = ConvertTo-SecureString $config.SecretProtected
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
$oldEnvironment = @{}
$envNames = @('SMS_ALLOWED_SENDERS','SMS_ALLOW_ALL_SENDERS','SMS_FILTER_MODE','SMS_FILTER_KEYWORD','WECHAT_TARGET','WECHAT_TARGET_TYPE','WECHAT_TARGET_VERIFIED',
    'SMS_REAL_SEND_APPROVED','SMS_DRY_RUN','SMS_BIND','SMS_ALLOWED_CLIENT_IPS','SMS_PORT','SMS_SECRET','SMS_DEDUP_DB','SMS_WECHAT_DATA_DIR')
try {
    foreach ($envName in $envNames) { $oldEnvironment[$envName] = [Environment]::GetEnvironmentVariable($envName, 'Process') }
    foreach ($property in $config.PSObject.Properties) {
        if ($envNames -contains $property.Name) { [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process') }
    }
    $env:SMS_FILTER_MODE = if ($null -eq $config.SMS_FILTER_MODE) { 'contains' } else { [string]$config.SMS_FILTER_MODE }
    $env:SMS_FILTER_KEYWORD = if ($null -eq $config.SMS_FILTER_KEYWORD) { -join @([char]0x9A8C,[char]0x8BC1,[char]0x7801) } else { [string]$config.SMS_FILTER_KEYWORD }
    $env:SMS_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    $env:SMS_WECHAT_DATA_DIR = $configRoot
    $dbName = if ($config.SMS_DRY_RUN -eq '1') { 'dry-run-fingerprints.sqlite3' } else { 'sent-fingerprints.sqlite3' }
    $env:SMS_DEDUP_DB = Join-Path $configRoot $dbName
    Set-Location -LiteralPath $PSScriptRoot
    & $pythonPath -u (Join-Path $PSScriptRoot 'bridge.py')
    if ($LASTEXITCODE -ne 0) { throw "Bridge stopped with exit code $LASTEXITCODE" }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    foreach ($envName in $envNames) { [Environment]::SetEnvironmentVariable($envName, $oldEnvironment[$envName], 'Process') }
}
