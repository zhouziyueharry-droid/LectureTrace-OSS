"""Download local Docling CPU assets only; never read or upload course documents.

Run: uv run --directory backend --no-sync python ../scripts/prepare_pdf_models.py
"""
import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path


def main():
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    from docling.utils.model_downloader import download_models
    target = Path(__file__).resolve().parents[1] / 'models' / 'docling'
    download_models(output_dir=target, with_layout=True, with_tableformer=True,
                    with_code_formula=False, with_picture_classifier=False,
                    with_rapidocr=True, rapidocr_models=['onnxruntime:ch'], progress=True)
    files = {}
    for path in sorted(target.rglob('*')):
        if path.is_file() and '.cache' not in path.parts and path.name != 'ready.json':
            with path.open('rb') as handle:
                files[path.relative_to(target).as_posix()] = hashlib.file_digest(handle, 'sha256').hexdigest()
    marker = {'docling': version('docling'), 'rapidocr': version('rapidocr'), 'files': files}
    pending = target / '.ready.tmp'
    pending.write_text(json.dumps(marker, sort_keys=True), encoding='utf-8')
    pending.replace(target / 'ready.json')
    print(f'Local PDF models ready: {target} ({len(files)} files)')


if __name__ == '__main__':
    main()
