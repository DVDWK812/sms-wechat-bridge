param(
    [string]$WindowsTailIP = '',
    [string]$PhoneTailIP = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configFile = Join-Path $configRoot 'config.json'
if (Test-Path -LiteralPath $configFile) {
    Write-Output 'Existing protected configuration preserved.'
    exit 0
}
foreach ($tailIP in @($WindowsTailIP, $PhoneTailIP)) {
    if ($tailIP) {
        $parsedIP = [Net.IPAddress]::Parse($tailIP)
        $octets = $parsedIP.GetAddressBytes()
        if ($octets.Length -ne 4 -or $octets[0] -ne 100 -or $octets[1] -lt 64 -or $octets[1] -gt 127) {
            throw 'Use only Tailscale IPv4 addresses.'
        }
    }
}
New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$userSID = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($userSID)
$acl.SetAccessRuleProtection($true, $false)
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$propagation = [Security.AccessControl.PropagationFlags]::None
$allow = [Security.AccessControl.AccessControlType]::Allow
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($userSID, 'FullControl', $inherit, $propagation, $allow)))
$systemSID = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($systemSID, 'FullControl', $inherit, $propagation, $allow)))
Set-Acl -LiteralPath $configRoot -AclObject $acl
$randomBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
$plainSecret = [Convert]::ToBase64String($randomBytes)
$protectedSecret = ConvertTo-SecureString -String $plainSecret -AsPlainText -Force | ConvertFrom-SecureString
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$config.SMS_BIND = $WindowsTailIP
$config.SMS_ALLOWED_CLIENT_IPS = $PhoneTailIP
$config | Add-Member -NotePropertyName SecretProtected -NotePropertyValue $protectedSecret
$config | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding UTF8
$plainSecret = $null
[Array]::Clear($randomBytes, 0, $randomBytes.Length)
Write-Output 'Created current-user DPAPI-protected secret and user/SYSTEM-only configuration. Dry run remains enabled.'
