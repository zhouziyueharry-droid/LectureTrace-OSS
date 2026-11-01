"""Video-owned immutable PDFs, page text and version-bound citations."""
from __future__ import annotations

import hashlib
import math
import json
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from importlib.metadata import version, PackageNotFoundError

from . import artifacts
from .config import settings
from .qa import notes

PDF_ID = r'[0-9a-f]{32}'
CITE_RE = re.compile(r'\[PDF:([^\]\s]+)\]')
_worker_slot = threading.BoundedSemaphore(1)


def root(video_id):
    # Reuse the existing video ID and symlink/hardlink boundary checks.
    base = notes._root(video_id).parent
    return notes._safe(settings.videos_dir, base / 'pdfs')


def folder(video_id, file_id, trash=False):
    if not re.fullmatch(PDF_ID, file_id or ''):
        raise notes.NoteError('PDF 标识无效')
    base = root(video_id)
    return notes._safe(base, base / ('.trash' if trash else '') / file_id)


def safe_file(directory, name):
    return notes._safe(directory.parent, directory / name)


def valid_name(name):
    if (not isinstance(name, str) or not name.lower().endswith('.pdf') or len(name) > 160
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or not name[:-4].strip()):
        raise notes.NoteError('请选择有效的 PDF 文件名')
    return name


def extract(source):
    if not 0 < source.stat().st_size <= settings.pdfs.max_bytes:
        raise notes.NoteError('PDF 超过大小上限或为空', 413)
    # Package staging paths can approach Windows MAX_PATH; keep worker output short.
    scratch = tempfile.TemporaryDirectory(prefix='lecturetrace-pdf-')
    output = Path(scratch.name) / 'pages.json'
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name('pdf_extract.py')),
                        str(source), str(output), json.dumps(asdict(settings.pdfs))],
                       timeout=settings.pdfs.timeout_s, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        if not output.exists() or output.stat().st_size > settings.pdfs.max_text_bytes * 2 + 10000:
            raise notes.NoteError('PDF 解析结果过大或不可用')
        result = json.loads(output.read_text(encoding='utf-8'))
        if 'error' in result:
            raise notes.NoteError(result['error'])
        return result['pages']
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        if isinstance(exc, notes.NoteError):
            raise
        raise notes.NoteError('PDF 解析失败或超时，请尝试较小的文件') from None
    finally:
        scratch.cleanup()


def metadata(video_id, file_id, revision=None):
    directory = folder(video_id, file_id)
    try:
        meta = artifacts.read_json(safe_file(directory, 'meta.json'))
        source = safe_file(directory, 'source.pdf')
        if not source.is_file():
            raise FileNotFoundError
    except (OSError, ValueError):
        raise notes.NoteError('PDF 不存在或已移入回收站', 404) from None
    if revision is not None and revision != meta['revision']:
        raise notes.NoteError('PDF 版本已变化，请重新选择', 409)
    # ponytail: hash originals on read (up to 32 MiB); cache verified hashes if many PDFs make this slow.
    with source.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    if source.stat().st_size != meta['size'] or digest != meta['revision']:
        raise notes.NoteError('PDF 原件已变化，请重新导入', 409)
    return meta


def listing(video_id):
    with notes.lock(video_id):
        base = root(video_id)
        return [metadata(video_id, p.name) for p in sorted(base.iterdir())
                if re.fullmatch(PDF_ID, p.name)] if base.exists() else []


def page_map(video_id, file_id, revision):
    """Compact navigation only; extracted text never proves a page is complete."""
    meta = metadata(video_id, file_id, revision)
    read_pages(video_id, file_id, revision, 1, 1, 'native')  # populate imported-package cache
    texts = artifacts.read_json(safe_file(folder(video_id, file_id), 'pages.json'))
    return {'id': file_id, 'revision': revision, 'page_count': meta['page_count'],
            'pages': [{'page': i, 'lead': ' '.join(text.split())[:100], 'text_chars': len(text)}
                      for i, text in enumerate(texts, 1)],
            'note': '文字层可能漏掉定义、公式和图示；长度仅供发现可疑页，不能证明页面完整。关键页请 view_pdf_page 核对原图。'}


def list_trash(video_id):
    with notes.lock(video_id):
        base = notes._safe(root(video_id), root(video_id) / '.trash')
        items = []
        for entry in sorted(base.iterdir()) if base.exists() else []:
            if not re.fullmatch(PDF_ID, entry.name):
                continue
            meta = artifacts.read_json(safe_file(folder(video_id, entry.name, True), 'meta.json'))
            items.append({'trash_id': entry.name, 'name': valid_name(meta['name'])})
        return items


def publish(video_id, directory, name):
    valid_name(name)
    source = directory / 'source.pdf'
    texts = extract(source)
    with source.open('rb') as handle:
        revision = hashlib.file_digest(handle, 'sha256').hexdigest()
    file_id = uuid.uuid4().hex
    meta = {'id': file_id, 'name': name, 'revision': revision, 'size': source.stat().st_size,
            'page_count': len(texts), 'text_pages': sum(bool(t) for t in texts)}
    artifacts.write_json(directory / 'meta.json', meta)
    artifacts.write_json(directory / 'pages.json', texts)
    with notes.lock(video_id):
        from .api.videos import get_video
        get_video(video_id)  # The video may have been deleted while uploading/parsing.
        directory.rename(folder(video_id, file_id))
    return meta


def delete(video_id, file_id):
    with notes.lock(video_id):
        metadata(video_id, file_id)
        target = folder(video_id, file_id, trash=True)
        target.parent.mkdir(exist_ok=True)
        folder(video_id, file_id).rename(target)
        return {'trash_id': file_id}


def restore(video_id, file_id):
    with notes.lock(video_id):
        src, dst = folder(video_id, file_id, True), folder(video_id, file_id)
        if not src.is_dir():
            raise notes.NoteError('回收站中没有此 PDF', 404)
        if dst.exists():
            raise notes.NoteError('此 PDF 已存在', 409)
        src.rename(dst)
        return metadata(video_id, file_id)


def read_pages(video_id, file_id, revision, start_page, end_page, mode='auto'):
    with notes.lock(video_id):
        if mode not in ('auto', 'native', 'ocr'):
            raise notes.NoteError('PDF 读取模式必须为 auto、native 或 ocr')
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
            raise notes.NoteError('请先列出 PDF，使用其完整 revision 读取')
        meta = metadata(video_id, file_id, revision)
        if (type(start_page) is not int or type(end_page) is not int
                or not 1 <= start_page <= end_page <= meta['page_count']
                or end_page - start_page + 1 > settings.pdfs.max_read_pages):
            raise notes.NoteError(f'页码范围无效；每次最多读取 {settings.pdfs.max_read_pages} 页')
        directory = folder(video_id, file_id)
        path = safe_file(directory, 'pages.json')
        if not path.exists():
            artifacts.write_json(path, extract(safe_file(directory, 'source.pdf')))
        if path.stat().st_size > settings.pdfs.max_text_bytes * 2 + 10000:
            raise notes.NoteError('PDF 文字缓存过大，请重新导入')
        texts = artifacts.read_json(path)
        rows = []
        for p in range(start_page, end_page + 1):
            native = texts[p - 1]
            row = {'id': f'PDF:{file_id}:{revision}:{p}', 'page': p,
                   'text': native, 'has_text': bool(native), 'source': 'native'}
            if mode != 'native':
                try:
                    parsed = _page_job(video_id, file_id, revision, {'kind': 'ocr', 'page': p},
                                       cached_only=bool(native) and mode == 'auto')
                    if parsed is not None:
                        row.update(parsed, native_text=native)
                        if not row['has_text'] and native:
                            row.update(text=native, has_text=True, source='native')
                except notes.NoteError as exc:
                    row['warning'] = str(exc)
            rows.append(row)
        return rows


def _cache_path(directory, revision, job):
    packages = ('pypdfium2',) if job['kind'] == 'image' else ('pypdfium2', 'docling', 'rapidocr', 'onnxruntime')
    versions = {}
    for name in packages:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = 'missing'
    marker = Path(settings.pdfs.docling_models_dir) / 'ready.json'
    fingerprint = {'format': 1, 'revision': revision, 'job': job, 'params': asdict(settings.pdfs),
                   'versions': versions, 'models': marker.read_text() if job['kind'] == 'ocr' and marker.is_file() else None}
    key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return safe_file(directory, '.agent-cache') / (key + '.json')


def _page_job(video_id, file_id, revision, job, cached_only=False):
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
        raise notes.NoteError('请先列出 PDF，使用其完整 revision 读取')
    meta = metadata(video_id, file_id, revision)
    if type(job['page']) is not int or not 1 <= job['page'] <= meta['page_count']:
        raise notes.NoteError('PDF 页码无效')
    directory = folder(video_id, file_id)
    cache = _cache_path(directory, revision, job)
    cached = _cached_page(directory, cache)
    if cached is not None:
        return cached
    if cached_only:
        return None
    max_result = settings.pdfs.max_image_bytes * 2 + settings.pdfs.max_text_bytes + 4096
    # ponytail: one bounded worker globally; a small process pool if measured contention warrants it.
    if not _worker_slot.acquire(timeout=settings.pdfs.worker_wait_s):
        raise notes.NoteError('PDF 正在处理其他页面，请稍后重试')
    output = directory / ('.read-' + uuid.uuid4().hex + '.json')
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name('pdf_read_worker.py')),
                        str(safe_file(directory, 'source.pdf')), str(output), json.dumps(job),
                        json.dumps(asdict(settings.pdfs))], check=True, timeout=settings.pdfs.worker_timeout_s,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        if not output.is_file() or output.stat().st_size > max_result:
            raise notes.NoteError('PDF 处理结果超过上限或不可用')
        result = artifacts.read_json(output)
        if 'error' in result:
            raise notes.NoteError(result['error'])
        metadata(video_id, file_id, revision)
        cache.parent.mkdir(exist_ok=True)
        entries = sorted(cache.parent.glob('*.json'), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in entries) + output.stat().st_size
        for entry in entries:
            if total <= settings.pdfs.max_cache_bytes:
                break
            safe_file(directory, entry.relative_to(directory))
            total -= entry.stat().st_size
            entry.unlink()
        if total <= settings.pdfs.max_cache_bytes:
            output.replace(cache)
        return result
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        if isinstance(exc, notes.NoteError):
            raise
        raise notes.NoteError('本地 PDF 处理失败或超时，请缩小读取范围') from None
    finally:
        output.unlink(missing_ok=True)
        _worker_slot.release()


def _cached_page(directory, cache):
    safe_file(directory, cache.relative_to(directory))
    if cache.is_file():
        if cache.stat().st_size > settings.pdfs.max_image_bytes * 2 + settings.pdfs.max_text_bytes + 4096:
            raise notes.NoteError('PDF 缓存超过上限')
        return artifacts.read_json(cache)
    return None


def view_page(video_id, file_id, revision, page, crop=None):
    if crop is not None and (not isinstance(crop, list) or len(crop) != 4
            or not all(type(v) in (int, float) and math.isfinite(v) for v in crop)
            or not 0 <= crop[0] < crop[2] <= 1 or not 0 <= crop[1] < crop[3] <= 1):
        raise notes.NoteError('裁剪范围须为左上角原点的归一化 [x0,y0,x1,y1]')
    with notes.lock(video_id):
        return _page_job(video_id, file_id, revision, {'kind': 'image', 'page': page, 'crop': crop})


def search_pages(video_id, file_id, revision, query, limit=8, snippet_chars=320):
    from .qa.workspace import _terms
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise notes.NoteError('PDF 查询须为 1–1000 字的非空文字')
    terms = set(_terms(query))
    with notes.lock(video_id):
        meta = metadata(video_id, file_id, revision)
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
            raise notes.NoteError('请先列出 PDF，使用其完整 revision 读取')
        directory = folder(video_id, file_id)
        # Generate native text once for imported course packages; never OCR implicitly during search.
        read_pages(video_id, file_id, revision, 1, 1, 'native')
        texts = artifacts.read_json(safe_file(directory, 'pages.json'))
        results, native_pages, ocr_pages, unindexed = [], [], [], []
        for page, native in enumerate(texts, 1):
            if native:
                native_pages.append(page)
            parsed = _cached_page(directory, _cache_path(directory, revision, {'kind': 'ocr', 'page': page}))
            if parsed is not None:
                ocr_pages.append(page)
            text = native + ('\n' + parsed['text'] if parsed else '')
            if not text.strip():
                unindexed.append(page)
            score = len(terms & set(_terms(text)))
            if score:
                positions = [text.casefold().find(t) for t in terms if t in text.casefold()]
                offset = max(0, min(positions) - 60) if positions else 0
                results.append({'page': page, 'score': score, 'excerpt': text[offset:offset + snippet_chars]})
        return {'results': sorted(results, key=lambda r: (-r['score'], r['page']))[:limit],
                'coverage': {'native_pages': native_pages, 'ocr_pages': ocr_pages, 'no_searchable_text': unindexed},
                'note': '摘录仅定位；未命中不等于不存在，图片中的内容可能未识别。引用前请 read_pdf 或 view_pdf_page。'}


def resolve(text, video_id, allowed=None):
    valid, invalid = [], []
    for raw in dict.fromkeys(CITE_RE.findall(text)):
        cite_id = 'PDF:' + raw
        try:
            file_id, revision, number = raw.split(':')
            if not re.fullmatch(r'[0-9a-f]{64}', revision) or not number.isdecimal():
                raise ValueError
            meta = metadata(video_id, file_id, revision)
            page = int(number)
            if not 1 <= page <= meta['page_count'] or (allowed is not None and cite_id not in allowed):
                raise ValueError
            valid.append({'id': cite_id, 'pdf_id': file_id, 'revision': revision, 'page': page, 'title': meta['name'],
                          'url': f'/api/videos/{video_id}/pdfs/{file_id}/file?revision={revision}#page={page}'})
        except (ValueError, notes.NoteError):
            invalid.append(cite_id)
    return valid, invalid
