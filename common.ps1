$configRoot = if ($env:SMS_WECHAT_DATA_DIR) { $env:SMS_WECHAT_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'SmsWechatBridgeOSS' }
if (-not [IO.Path]::IsPathRooted($configRoot)) { throw 'Data directory must be absolute.' }
$env:SMS_WECHAT_DATA_DIR = $configRoot
