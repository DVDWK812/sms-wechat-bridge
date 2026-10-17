param([ValidateSet('Enable','Disable','Status','Validate')][string]$Mode='Status')
$ErrorActionPreference = 'Stop'
$trayIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'SmsWechatBridgeOSS-Tray-' + $trayIdentity.User.Value
$trayPython = Join-Path $PSScriptRoot '.venv-wechat\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $trayPython)) { throw 'Sender Python environment missing.' }
$arguments = '-X utf8 "' + (Join-Path $PSScriptRoot 'tray_app.py') + '"'
if ($Mode -eq 'Status') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Output ('Enabled=' + $(if ($task -and $task.State -ne 'Disabled') { 'true' } else { 'false' }))
    exit 0
}
if ($Mode -eq 'Disable') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output 'Enabled=false'
    exit 0
}
$action = New-ScheduledTaskAction -Execute $trayPython -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $trayIdentity.Name
$trigger.Delay = 'PT15S'
$principal = New-ScheduledTaskPrincipal -UserId $trayIdentity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($Mode -eq 'Validate') {
    Write-Output (@{Execute=$action.Execute;Arguments=$action.Arguments;User=$principal.UserId;
        LogonType=[string]$principal.LogonType;RunLevel=[string]$principal.RunLevel;Delay=$trigger.Delay;
        ExecutionTimeLimit=$settings.ExecutionTimeLimit;RestartCount=$settings.RestartCount;
        MultipleInstances=[string]$settings.MultipleInstances} | ConvertTo-Json)
    exit 0
}
if (Get-ScheduledTask -TaskName 'SmsWechatBridgeOSS-Interactive' -ErrorAction SilentlyContinue) {
    throw 'An old direct bridge logon task exists. Review/remove it before enabling tray logon startup.'
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output 'Enabled=true'
