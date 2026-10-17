"""Download author-official local OCR weights. No desktop reads or messages."""
import os
from pathlib import Path

os.environ['PADDLE_PDX_MODEL_SOURCE']='bos'
from paddleocr import PaddleOCR

from app_paths import MODEL_ROOT
root=MODEL_ROOT
root.mkdir(parents=True,exist_ok=True)
PaddleOCR(text_detection_model_name='PP-OCRv5_mobile_det',text_recognition_model_name='PP-OCRv5_mobile_rec',
    use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,
    device='cpu',cpu_threads=2,enable_mkldnn=False)
cache=Path.home()/'.paddlex'/'official_models'
import shutil
for name in ('PP-OCRv5_mobile_det','PP-OCRv5_mobile_rec'):
    source=cache/name
    destination=root/name
    if destination.exists():
        raise SystemExit('Existing model directory must be reviewed; refusing overwrite')
    shutil.copytree(source,destination)
print('Official local OCR models prepared; no desktop read or message sent.')
