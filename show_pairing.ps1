# Displays the secret locally for manual entry. No terminal output or clipboard.
Add-Type -AssemblyName System.Windows.Forms
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$configFile = Join-Path $configRoot 'config.json'
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$form = New-Object Windows.Forms.Form
$form.Text = 'SmsForwarder pairing - manual entry only'
$form.Width = 720
$form.Height = 330
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$info = New-Object Windows.Forms.Label
$info.SetBounds(20, 15, 660, 120)
$info.Text = "POST URL: http://$($config.SMS_BIND):$($config.SMS_PORT)/sms`r`nmessage template: from=[from]&content=[org_content]&timestamp=[timestamp]&sign=[sign]`r`nSender exact match: $($config.SMS_ALLOWED_SENDERS)`r`nDry run: $($config.SMS_DRY_RUN)`r`nSecret below: enter only into your phone. Do not send it in chat."
$form.Controls.Add($info)
$secretBox = New-Object Windows.Forms.TextBox
$secretBox.SetBounds(20, 145, 650, 28)
$secretBox.ReadOnly = $true
$secretBox.Font = New-Object System.Drawing.Font('Consolas', 15)
$secretBox.UseSystemPasswordChar = $true
$secureSecret = ConvertTo-SecureString $config.SecretProtected
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
try { $secretBox.Text = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
$form.Controls.Add($secretBox)
$reveal = New-Object Windows.Forms.CheckBox
$reveal.SetBounds(20, 185, 620, 28)
$reveal.Text = 'Show secret on this screen for manual entry (no clipboard)'
$reveal.Add_CheckedChanged({ $secretBox.UseSystemPasswordChar = -not $reveal.Checked })
$form.Controls.Add($reveal)
try { [void]$form.ShowDialog() }
finally { $secretBox.Text = ''; $form.Dispose(); $secureSecret.Dispose() }
