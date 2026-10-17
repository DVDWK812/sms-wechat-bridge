"""Publication runtime is isolated from any existing deployment."""
import os
from pathlib import Path
PROJECT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('SMS_WECHAT_DATA_DIR') or (Path(os.environ.get('LOCALAPPDATA', str(PROJECT / '.local'))) / 'SmsWechatBridgeOSS'))
if not ROOT.is_absolute():
    raise RuntimeError('data_directory_must_be_absolute')
MODEL_ROOT = Path(os.environ.get('SMS_OCR_MODEL_DIR') or ROOT / 'paddle-models')
VERSION = '4.1.15.13'
