param(
    [Parameter(Mandatory=$true)][string]$WindowsTailIP,
    [Parameter(Mandatory=$true)][string]$PhoneTailIP,
    [ValidateRange(1024,65535)][int]$Port=8765,
    [switch]$RepairInterpreter
)
$ErrorActionPreference = 'Stop'
foreach ($tailIP in @($WindowsTailIP, $PhoneTailIP)) {
    $octets = ([Net.IPAddress]::Parse($tailIP)).GetAddressBytes()
    if ($octets.Length -ne 4 -or $octets[0] -ne 100 -or $octets[1] -lt 64 -or $octets[1] -gt 127) { throw 'Only exact Tailscale IPv4 allowed.' }
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'User must approve an administrator PowerShell window to apply this firewall rule.'
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
# Windows venv python.exe may delegate to the base interpreter. Firewall must
# match the executable owning the listening socket, not the launcher.
$pythonPath = (& $venvPython -c 'import sys; print(sys._base_executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Cannot identify the actual Python interpreter.'
}
$ruleName = ('SmsWechatBridgeOSS-PhoneOnly-' + $Port)
$existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existingRule) {
    if (-not $RepairInterpreter) { throw 'Existing rule must be reviewed before replacement.' }
    $addresses = $existingRule | Get-NetFirewallAddressFilter
    $ports = $existingRule | Get-NetFirewallPortFilter
    if (@($addresses.LocalAddress).Count -ne 1 -or $addresses.LocalAddress -ne $WindowsTailIP -or
        @($addresses.RemoteAddress).Count -ne 1 -or $addresses.RemoteAddress -ne $PhoneTailIP -or
        $ports.LocalPort -ne [string]$Port -or $ports.Protocol -ne 'TCP' -or
        $existingRule.Direction -ne 'Inbound' -or $existingRule.Action -ne 'Allow') {
        throw 'Existing rule does not match the reviewed phone-only scope; stop for review.'
    }
    $existingRule | Get-NetFirewallApplicationFilter | Set-NetFirewallApplicationFilter -Program $pythonPath | Out-Null
    Write-Output 'Corrected the interpreter path; exact phone, listener and port unchanged.'
    exit 0
}
New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $WindowsTailIP -LocalPort $Port -RemoteAddress $PhoneTailIP -Program $pythonPath -Profile Any | Out-Null
Write-Output 'Created TCP 8765 rule restricted to the configured phone and Tailscale listener.'
