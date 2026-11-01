"""Portable lecture/course snapshots. Never copy credentials or enqueue processing."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import artifacts, db
from . import pdfs
from .config import SCHEMA_VERSION, settings
from .pipeline import roi, runner
from .pipeline.base import STAGES, STAGE_OUTPUT
from .package_validation import validate_json
from .qa import notes, outline_edits

_guard = threading.RLock()  # Single-process local app: serialize staging/cleanup/commit.
ROOT_FILES = {
    'source.mp4', 'audio.wav', 'audio.json', 'transcript.json', 'cues.json',
    'keyframes.json', 'keyframes_debug.json', 'segments.json', 'slides.json',
    'evidence.json', 'outline.json', 'stage_meta.json', 'translate.json',
    'subs.source.vtt', 'subs.bilingual.vtt',
}


class PackageError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Video(Model):
    id: str = Field(pattern=r'^[0-9a-f]{12}$')
    title: str = Field(min_length=1, max_length=1000)
    filename: str = Field(min_length=1, max_length=1000)
    duration_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    created_at: str = Field(max_length=100)


class Course(Model):
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    title: str = Field(min_length=1, max_length=120)


class Stage(Model):
    name: str
    status: Literal['done', 'skipped']
    skip_reason: Literal['未开启大纲生成', '暂未启用', '已停用'] | None = None


class Entry(Model):
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Manifest(Model):
    format: Literal['lecturetrace-course'] = 'lecturetrace-course'
    version: Literal[1, 2] = 1
    video: Video
    course: Course | None = None
    stages: list[Stage]
    roi: list[float] | None = None
    files: dict[str, Entry]


class CourseManifest(Model):
    format: Literal['lecturetrace-course'] = 'lecturetrace-course'
    version: Literal[3] = 3
    course: Course
    lectures: list[Manifest] = Field(min_length=1)


def _lectures(m: Manifest | CourseManifest):
    return [(f'videos/{v.video.id}/', v) for v in m.lectures] if isinstance(m, CourseManifest) else [('', m)]


def _files(m: Manifest | CourseManifest):
    return {prefix + name: entry for prefix, v in _lectures(m) for name, entry in v.files.items()}


def allowed_path(name: str) -> bool:
    parts = name.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part != part.rstrip(' .')
                or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]', part.split('.')[0], re.I)):
            return False
    if name in ROOT_FILES:
        return True
    if re.fullmatch(r'pdfs/(?:\.trash/)?[0-9a-f]{32}/(?:source\.pdf|meta\.json)', name):
        return True
    if re.fullmatch(r'keyframes/S\d{3,6}(?:_\d+)?\.jpg', name):
        return True
    if parts[0] != 'notes':
        return False
    if len(parts) == 2:
        try:
            notes.valid_name(parts[1])
            return True
        except notes.NoteError:
            return False
    # Only the existing notes storage formats, never arbitrary nested files.
    if len(parts) == 3 and parts[1] in ('.history', '.proposals'):
        return parts[2].endswith('.md.json')
    if len(parts) == 4 and parts[1] == '.history' and parts[2].endswith('.md.v'):
        return bool(re.fullmatch(r'\d+-[0-9a-f]{8}\.json', parts[3]))
    if len(parts) >= 4 and parts[1] == '.trash' and re.fullmatch(r'\d+-[0-9a-f]{8}', parts[2]):
        return ((len(parts) == 4 and parts[3] in ('note.md', 'meta.json', 'proposal.json', 'info.json'))
                or (len(parts) == 5 and parts[3] == 'versions'
                    and bool(re.fullmatch(r'\d+-[0-9a-f]{8}\.json', parts[4]))))
    return False


def staging_root() -> Path:
    root = settings.data_dir / '.course-packages'
    if root.is_symlink() or root.is_junction():
        raise PackageError('课程包暂存目录不能是链接')
    root.mkdir(parents=True, exist_ok=True)
    return root


def stage_path(token: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise PackageError('课程包不存在或已过期', 404)
    path = staging_root() / token
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise PackageError('课程包不存在或已过期', 404)
    return path


def discard(token: str):
    with _guard:
        path = stage_path(token)
        # Only server-created staging folders with our marker may be removed.
        if not (path / 'owner').is_file() or (path / 'owner').read_text() != token:
            raise PackageError('课程包暂存记录无效')
        shutil.rmtree(path)


def new_stage() -> tuple[str, Path]:
    with _guard:
        root = staging_root()
        for path in root.iterdir():
            if (re.fullmatch(r'[0-9a-f]{32}', path.name) and not path.is_symlink()
                    and not path.is_junction() and path.is_dir()
                    and time.time() - path.stat().st_mtime > settings.packages.ttl_s):
                discard(path.name)
        token = uuid.uuid4().hex
        path = root / token
        path.mkdir()
        (path / 'owner').write_text(token)
        return token, path


def _read_manifest(raw: bytes) -> Manifest | CourseManifest:
    if len(raw) > settings.packages.max_manifest_bytes:
        raise PackageError('课程包清单过大')
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and value.get('version') == 3:
            m = CourseManifest.model_validate_json(raw)
            if len({v.video.id for v in m.lectures}) != len(m.lectures):
                raise PackageError('课程包包含重复讲次')
            for lecture in m.lectures:
                _read_manifest(lecture.model_dump_json().encode())
                if lecture.course != m.course:
                    raise PackageError('课程包讲次归属不一致')
            files = _files(m)
            if len(files) > settings.packages.max_files or sum(f.size for f in files.values()) > settings.packages.max_expanded_bytes:
                raise PackageError('课程包文件数量或解压大小超过上限')
            return m
        m = Manifest.model_validate_json(raw)
    except PackageError:
        raise
    except (ValidationError, ValueError, RecursionError):
        raise PackageError('课程包格式无效或版本不受支持') from None
    if len(m.files) > settings.packages.max_files or sum(f.size for f in m.files.values()) > settings.packages.max_expanded_bytes:
        raise PackageError('课程包文件数量或解压大小超过上限')
    keys = set()
    for name in m.files:
        if name.startswith('pdfs/'):
            if m.version != 2:
                raise PackageError('包含 PDF 的课程包需要格式版本 2')
            partner = name.rsplit('/', 1)[0] + ('/meta.json' if name.endswith('/source.pdf') else '/source.pdf')
            if partner not in m.files:
                raise PackageError('课程包 PDF 原件与元信息不完整')
            if name.endswith('.pdf') and m.files[name].size > settings.pdfs.max_bytes:
                raise PackageError('课程包 PDF 超过大小上限')
        key = unicodedata.normalize('NFC', name).casefold()
        if not allowed_path(name) or key in keys:
            raise PackageError('课程包包含不支持或冲突的文件路径')
        keys.add(key)
    if {s.name for s in m.stages} != set(STAGES) or len(m.stages) != len(STAGES):
        raise PackageError('课程包处理阶段不完整')
    required = {'source.mp4'}
    for s in m.stages:
        if s.status == 'done' and s.name in STAGE_OUTPUT:
            required.add(STAGE_OUTPUT[s.name])
            if s.name == 'translate':
                required.update(('subs.source.vtt', 'subs.bilingual.vtt'))
    if not required.issubset(m.files):
        raise PackageError('课程包缺少视频或已完成阶段的结果文件')
    if m.files['source.mp4'].size == 0:
        raise PackageError('课程包原视频为空')
    if m.roi is not None:
        if (len(m.roi) != 4 or any(not math.isfinite(n) or n < 0 or n > 1 for n in m.roi)
                or m.roi[2] <= 0 or m.roi[3] <= 0 or m.roi[0] + m.roi[2] > 1.0001
                or m.roi[1] + m.roi[3] > 1.0001):
            raise PackageError('课程包的画面区域无效')
    return m


def _validate_data(root: Path, m: Manifest):
    def images(value):
        if isinstance(value, dict):
            for key, v in value.items():
                if key == 'image' and isinstance(v, str) and v not in m.files:
                    raise PackageError('课程包缺少引用的幻灯片图片')
                if key == 'appearance_images' and isinstance(v, list) and any(x not in m.files for x in v):
                    raise PackageError('课程包缺少出现截图')
                images(v)
        elif isinstance(value, list):
            for v in value:
                images(v)
    for name in m.files:
        if name.startswith('pdfs/'):
            if not name.endswith('/meta.json'):
                continue
            directory = root / name.rsplit('/', 1)[0]
            if (directory / 'meta.json').stat().st_size > 4096:
                raise PackageError('PDF 元信息过大')
            try:
                meta = artifacts.read_json(directory / 'meta.json')
                pdfs.valid_name(meta['name'])
                texts = pdfs.extract(directory / 'source.pdf')
                entry = m.files[name.rsplit('/', 1)[0] + '/source.pdf']
                if (meta['id'] != directory.name or meta['revision'] != entry.sha256 or meta['size'] != entry.size
                        or meta['page_count'] != len(texts) or meta['text_pages'] != sum(bool(t) for t in texts)):
                    raise ValueError
            except (ValueError, KeyError, TypeError, OSError):
                raise PackageError('课程包 PDF 格式或元信息无效') from None
            # Never accept external extracted text as evidence. Rebuild from the original lazily.
            continue
        if not name.endswith(('.json', '.md', '.vtt')):
            continue
        path = root / name
        if path.stat().st_size > settings.packages.max_metadata_bytes:
            raise PackageError('课程包单个文本文件过大')
        try:
            text = path.read_text(encoding='utf-8')
            if name.endswith('.json'):
                value = json.loads(text)
                validate_json(name, value, set(m.files))
                if name in ROOT_FILES and name != 'stage_meta.json':
                    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION or 'data' not in value:
                        raise PackageError('课程包产物格式或版本不受支持')
                    data = value['data']
                    if name in ('cues.json', 'keyframes.json') and not isinstance(data, list):
                        raise PackageError('课程包列表产物格式无效')
                    for file, keys in {'segments.json': ('sections', 'speech'), 'evidence.json': ('pages', 'sections', 'speech'),
                                       'outline.json': ('chapters',), 'transcript.json': ('segments',)}.items():
                        if name == file and (not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in keys)):
                            raise PackageError('课程包结构化产物格式无效')
                    images(data)
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            if isinstance(exc, PackageError):
                raise
            raise PackageError('课程包文本或 JSON 无效') from None


def _duplicate(video_id: str) -> bool:
    return bool(db.fetch_one('SELECT id FROM videos WHERE id=?', (video_id,))) or (settings.videos_dir / video_id).exists()


def summary(m: Manifest | CourseManifest, token: str) -> dict:
    if isinstance(m, CourseManifest):
        lectures = [summary(v, token) for v in m.lectures]
        return {'token': token, 'course_id': m.course.id, 'title': m.course.title,
                'course_title': m.course.title, 'has_video': True, 'lectures': lectures,
                'size_bytes': sum(v['size_bytes'] for v in lectures),
                'note_count': sum(v['note_count'] for v in lectures),
                'contents': list(dict.fromkeys(c for v in lectures for c in v['contents'])),
                'duplicate': any(v['duplicate'] for v in lectures)}
    labels = [('subs.source.vtt', '原文字幕'), ('subs.bilingual.vtt', '双语字幕'), ('keyframes.json', '幻灯片'),
              ('segments.json', '切分结果'), ('evidence.json', '问答证据'), ('outline.json', '大纲')]
    return {'token': token, 'video_id': m.video.id, 'title': m.video.title,
            'course_title': m.course.title if m.course else None, 'has_video': True,
            'size_bytes': sum(f.size for f in m.files.values()),
            'contents': [label for name, label in labels if name in m.files] +
                        (['PDF 资料'] if any(n.startswith('pdfs/') for n in m.files) else []),
            'note_count': sum(n.startswith('notes/') and n.count('/') == 1 for n in m.files),
            'duplicate': _duplicate(m.video.id)}


def inspect_archive(token: str) -> dict:
    with _guard:
        directory = stage_path(token)
        archive = directory / 'upload.zip'
        if archive.stat().st_size > settings.packages.max_archive_bytes:
            raise PackageError('课程包超过大小上限', 413)
        payload = directory / 'payload'
        payload.mkdir()
        try:
            with zipfile.ZipFile(archive) as z:
                infos = z.infolist()
                names = [i.filename for i in infos]
                if len(names) > settings.packages.max_files + 1 or len(set(names)) != len(names):
                    raise PackageError('课程包文件过多或包含重复条目')
                if 'manifest.json' not in names or z.getinfo('manifest.json').file_size > settings.packages.max_manifest_bytes:
                    raise PackageError('缺少有效课程包清单')
                m = _read_manifest(z.read('manifest.json'))
                files = _files(m)
                if set(names) != set(files) | {'manifest.json'}:
                    raise PackageError('课程包文件与清单不一致')
                for info in infos:
                    mode = info.external_attr >> 16
                    if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1:
                        raise PackageError('课程包不能包含链接、目录或加密条目')
                    if info.filename == 'manifest.json':
                        continue
                    entry = files[info.filename]
                    if info.file_size != entry.size:
                        raise PackageError('课程包文件大小与清单不一致')
                    if info.filename.endswith(('.json', '.md', '.vtt')) and entry.size > settings.packages.max_metadata_bytes:
                        raise PackageError('课程包单个文本文件过大')
                    target = payload / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with z.open(info) as source, target.open('xb') as out:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            if size > entry.size:
                                raise PackageError('课程包解压大小异常')
                            out.write(chunk)
                            digest.update(chunk)
                    if size != entry.size or digest.hexdigest() != entry.sha256:
                        raise PackageError('课程包校验失败，文件可能损坏')
            for prefix, lecture in _lectures(m):
                _validate_data(payload / prefix, lecture)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError):
            raise PackageError('无法读取课程包，请选择 LectureTrace 导出的完整文件') from None
        (directory / 'manifest.json').write_text(m.model_dump_json(), encoding='utf-8')
        archive.unlink()
        return summary(m, token)


def _snapshot(video_id: str):
    v = db.fetch_one('SELECT * FROM videos WHERE id=?', (video_id,))
    if not v:
        raise PackageError('视频不存在', 404)
    rows = db.fetch_all('SELECT * FROM stages WHERE video_id=? ORDER BY name', (video_id,))
    if v['status'] != 'done' or runner.is_active(video_id) or any(s['status'] not in ('done', 'skipped') for s in rows):
        raise PackageError('请等待视频处理完成后再导出课程包', 409)
    return v, rows


def _course_snapshot(course_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}', course_id):
        raise PackageError('课程不存在', 404)
    course = db.fetch_one('SELECT id,title FROM courses WHERE id=?', (course_id,))
    if not course:
        raise PackageError('课程不存在', 404)
    videos = db.fetch_all('SELECT id,title FROM videos WHERE course_id=? ORDER BY created_at, rowid', (course_id,))
    if not videos:
        raise PackageError('课程还没有讲次，无法导出', 409)
    for video in videos:
        try:
            _snapshot(video['id'])
        except PackageError as exc:
            raise PackageError(f"「{video['title']}」：{exc}", exc.status_code) from None
    return course, [v['id'] for v in videos]


def export(video_id: str, on_progress=None) -> dict:
    if not re.fullmatch(r'[0-9a-f]{12}', video_id):
        raise PackageError('视频不存在', 404)
    return _export([video_id], on_progress=on_progress)


def export_course(course_id: str, on_progress=None) -> dict:
    with _guard:
        course, video_ids = _course_snapshot(course_id)
        return _export(video_ids, course, on_progress)


def _export(video_ids: list[str], course: dict | None = None, on_progress=None) -> dict:
    progress = {'phase': 'scanning', 'written_bytes': 0, 'total_bytes': 0,
                'lecture_index': 0, 'lecture_count': len(video_ids), 'lecture_title': ''}

    def report(phase):
        progress['phase'] = phase
        if on_progress:
            on_progress(dict(progress))

    token, directory = new_stage()
    try:
        with _guard, ExitStack() as locks:
            for video_id in sorted(video_ids):
                locks.enter_context(notes.lock(video_id))
                locks.enter_context(outline_edits.lock(video_id))
            before = {v: _snapshot(v) for v in video_ids}
            report('scanning')
            paths = {}
            for video_id in video_ids:
                root = artifacts.video_dir(video_id)
                if root.is_symlink() or root.is_junction() or settings.videos_dir.is_symlink() or settings.videos_dir.is_junction():
                    raise PackageError('视频目录不能包含链接')
                notes.import_all_legacy(video_id)
                prefix = f'videos/{video_id}/' if course else ''
                for path in root.rglob('*'):
                    name = path.relative_to(root).as_posix()
                    if path.is_symlink() or path.is_junction():
                        raise PackageError('视频目录包含链接，无法安全导出')
                    if path.is_file() and allowed_path(name):
                        if path.stat().st_nlink > 1:
                            raise PackageError('视频目录包含硬链接，无法安全导出')
                        paths[prefix + name] = path
            if len(paths) > settings.packages.max_files or sum(p.stat().st_size for p in paths.values()) > settings.packages.max_archive_bytes:
                raise PackageError('课程包超过大小或文件数量上限', 413)
            signatures = {n: (p.stat().st_size, p.stat().st_mtime_ns) for n, p in paths.items()}
            progress['total_bytes'] = sum(size for size, _ in signatures.values())
            entries = {}
            archive = directory / 'export.ltcourse'
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
                for name, path in paths.items():
                    video_id = name.split('/')[1] if course else video_ids[0]
                    progress.update(lecture_index=video_ids.index(video_id) + 1,
                                    lecture_title=before[video_id][0]['title'])
                    report('writing')
                    digest = hashlib.sha256()
                    size = 0
                    with path.open('rb') as source, z.open(name, 'w', force_zip64=True) as out:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            if size > signatures[name][0]:
                                raise PackageError('导出期间内容发生变化，请重试', 409)
                            digest.update(chunk)
                            out.write(chunk)
                            progress['written_bytes'] += len(chunk)
                            report('writing')
                    entries[name] = Entry(size=size, sha256=digest.hexdigest())
                manifests = []
                for video_id in video_ids:
                    v, stages = before[video_id]
                    progress.update(lecture_index=video_ids.index(video_id) + 1, lecture_title=v['title'])
                    report('verifying')
                    prefix = f'videos/{video_id}/' if course else ''
                    files = {n[len(prefix):]: e for n, e in entries.items() if n.startswith(prefix)}
                    owner = course or (db.fetch_one('SELECT id,title FROM courses WHERE id=?', (v['course_id'],)) if v['course_id'] else None)
                    m = Manifest(version=2 if any(n.startswith('pdfs/') for n in files) else 1,
                                 video=Video(**{k: v[k] for k in Video.model_fields}),
                                 course=Course(**owner) if owner else None,
                                 stages=[Stage(name=s['name'], status=s['status'],
                                               skip_reason=s['error'] if s['status'] == 'skipped' and s['error'] in ('未开启大纲生成', '暂未启用', '已停用') else None)
                                         for s in stages],
                                 roi=roi.effective(video_id) or list(roi.FULL_FRAME), files=files)
                    _validate_data(artifacts.video_dir(video_id), m)
                    manifests.append(m)
                manifest = CourseManifest(course=Course(**course), lectures=manifests) if course else manifests[0]
                manifest = _read_manifest(manifest.model_dump_json().encode())
                z.writestr('manifest.json', manifest.model_dump_json())
            if (before != {v: _snapshot(v) for v in video_ids}
                    or any(sig != (paths[n].stat().st_size, paths[n].stat().st_mtime_ns) for n, sig in signatures.items())
                    or (course and (course, video_ids) != _course_snapshot(course['id']))):
                raise PackageError('导出期间内容发生变化，请重试', 409)
            if archive.stat().st_size > settings.packages.max_archive_bytes:
                raise PackageError('课程包超过大小上限', 413)
            filename = course['id'] if course else video_ids[0]
            return {'token': token, 'download_url': f'/api/packages/{token}/download', 'filename': f'{filename}.ltcourse'}
    except BaseException:
        discard(token)
        raise


def commit(token: str) -> dict:
    with _guard:
        directory = stage_path(token)
        m = _read_manifest((directory / 'manifest.json').read_bytes())
        lectures = _lectures(m)
        settings.videos_dir.mkdir(parents=True, exist_ok=True)
        if settings.videos_dir.is_symlink() or settings.videos_dir.is_junction():
            raise PackageError('视频目录不能是链接')
        moved = []
        try:
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                for _, lecture in lectures:
                    if _duplicate(lecture.video.id):
                        raise PackageError(f'「{lecture.video.title}」已存在，课程包不会覆盖已有内容', 409)
                course_id = None
                if m.course:
                    existing = conn.execute('SELECT title FROM courses WHERE id=?', (m.course.id,)).fetchone()
                    course_id = m.course.id if not existing or existing['title'] == m.course.title else uuid.uuid4().hex
                    conn.execute('INSERT OR IGNORE INTO courses VALUES (?,?,?)', (course_id, m.course.title, db.now_iso()))
                for prefix, lecture in lectures:
                    v = lecture.video
                    conn.execute('INSERT INTO videos (id,title,filename,duration_s,created_at,status,course_id) VALUES (?,?,?,?,?,?,?)',
                                 (v.id, v.title, v.filename, v.duration_s, v.created_at, 'done', course_id))
                    conn.executemany('INSERT INTO stages (video_id,name,status,finished_at,attempt,error) VALUES (?,?,?,?,?,?)',
                                     [(v.id, s.name, s.status, db.now_iso(), 0, s.skip_reason) for s in lecture.stages])
                    if lecture.roi is not None:
                        conn.execute('INSERT INTO app_settings VALUES (?,?)',
                                     (roi.confirmed_key(v.id), json.dumps({'roi': lecture.roi, 'confirmed_at': db.now_iso()})))
                    source = directory / 'payload' / prefix
                    target = settings.videos_dir / v.id
                    source.rename(target)
                    moved.append((source, target))
        except BaseException:
            for source, target in reversed(moved):
                target.rename(source)
            raise
        try:
            discard(token)
        except OSError:
            logging.getLogger(__name__).warning('课程包已导入，暂存记录将在下次清理时回收')
        if isinstance(m, CourseManifest):
            return {'course_id': course_id, 'video_ids': [v.video.id for _, v in lectures]}
        return {'video_id': m.video.id}
