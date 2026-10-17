$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$oldData = $env:SMS_WECHAT_DATA_DIR
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('sms-oss-smoke-' + [guid]::NewGuid().ToString('N'))
$trayProcess = $null
try {
    $env:SMS_WECHAT_DATA_DIR = $testRoot
    # Parse every shipped PowerShell script without executing installers/settings.
    Get-ChildItem -LiteralPath $project -Filter '*.ps1' | ForEach-Object {
        $parseTokens = $null; $parseErrors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$parseTokens,[ref]$parseErrors)
        if ($parseErrors.Count) { throw ('PowerShell parse failed: ' + $_.Name) }
    }
    & (Join-Path $project 'initialize_config.ps1')
    $configFile = Join-Path $testRoot 'config.json'
    $before = (Get-FileHash -LiteralPath $configFile).Hash
    & (Join-Path $project 'initialize_config.ps1')
    if ($before -ne (Get-FileHash -LiteralPath $configFile).Hash) { throw 'Initializer overwrote config.' }
    $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.SMS_DRY_RUN -ne '1' -or $config.SMS_REAL_SEND_APPROVED -ne '0' -or $config.WECHAT_TARGET_VERIFIED -ne '0') { throw 'Default gates not off.' }
    # Decrypt only newly generated temporary DPAPI data; never print it.
    $secure = ConvertTo-SecureString $config.SecretProtected
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Length -lt 32) { throw 'DPAPI roundtrip failed.' }
    } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secure.Dispose() }
    & (Join-Path $project 'set_tray_autostart.ps1') -Mode Validate | Out-Null
    # Start the real detached tray OFF, in an isolated temporary data root.
    & (Join-Path $project 'start_tray.ps1')
    $statusFile = Join-Path $testRoot 'tray-status.json'
    $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
    $trayProcess = Get-Process -Id $status.pid
    if ($status.status -ne 'off' -or $status.desired_enabled -ne $false -or $status.receiver_pid) { throw 'Unexpected active listener.' }
    & (Join-Path $project 'start_tray.ps1')
    $again = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
    if ($again.pid -ne $status.pid) { throw 'Duplicate tray instance.' }
    # Address only the hidden window belonging to this captured test process.
    $env:SMS_SMOKE_TRAY_PID = [string]$status.pid
    $script = @'
import os,time
import win32gui,win32process,win32con
pid=int(os.environ['SMS_SMOKE_TRAY_PID'])
windows=[]
def visit(hwnd,_):
    if win32process.GetWindowThreadProcessId(hwnd)[1]==pid and win32gui.GetClassName(hwnd)=='SmsWechatBridgeTrayWindow':
        windows.append(hwnd)
win32gui.EnumWindows(visit,None)
assert len(windows)==1
win32gui.PostMessage(windows[0],win32con.WM_COMMAND,105,0)
'@
    $script | & (Join-Path $project '.venv-wechat\Scripts\python.exe') -
    if ($LASTEXITCODE -ne 0) { throw 'Test tray shutdown failed.' }
    if (-not $trayProcess.WaitForExit(5000)) { throw 'Tray exit timeout.' }
    Write-Output 'PASS: script parse, isolated config/DPAPI, detached OFF tray, singleton, graceful exit, task definition (not registered); real sends=0.'
} finally {
    if ($trayProcess -and -not $trayProcess.HasExited) { Stop-Process -Id $trayProcess.Id -ErrorAction SilentlyContinue }
    $env:SMS_WECHAT_DATA_DIR = $oldData
    Remove-Item Env:SMS_SMOKE_TRAY_PID -ErrorAction SilentlyContinue
    # Test artifacts remain in the system temp directory for local inspection.
}
