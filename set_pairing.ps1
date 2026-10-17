param([Parameter(Mandatory=$true)][string]$PhoneTailIP)
$ErrorActionPreference = 'Stop'
$tailCli = if ($env:TAILSCALE_EXE) { $env:TAILSCALE_EXE } else { Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe' }
$state = & $tailCli status --json | ConvertFrom-Json
if ($state.BackendState -ne 'Running') { throw 'Windows Tailscale is not connected.' }
$phone = @($state.Peer.PSObject.Properties.Value | Where-Object { $_.OS -eq 'android' -and $_.TailscaleIPs -contains $PhoneTailIP })
if ($phone.Count -ne 1 -or -not $phone[0].Online) { throw 'Specified Android peer is not uniquely identified and online.' }
$windowsIP = @($state.Self.TailscaleIPs | Where-Object { $_ -match '^100\.' })
if ($windowsIP.Count -ne 1) { throw 'Cannot determine a single local Tailscale IPv4.' }
. (Join-Path $PSScriptRoot 'common.ps1')
$configFile = Join-Path $configRoot 'config.json'
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$config.SMS_BIND = $windowsIP[0]
$config.SMS_ALLOWED_CLIENT_IPS = $PhoneTailIP
$config | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding UTF8
Write-Output 'Configured exact local Tailscale IP and explicitly identified Android peer. Secret and dry-run setting preserved.'
