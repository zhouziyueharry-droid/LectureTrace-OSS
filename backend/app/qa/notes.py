"""视频级 Markdown 笔记。UI 与 Agent 共用版本校验和一次撤销。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import artifacts
from ..config import settings
from ..pipeline.evidence import id_index
from . import citations, context, outline_edits

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


class NoteError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def lock(video_id):
    with _guard:
        return _locks.setdefault(video_id, threading.RLock())


def valid_name(name):
    if (not isinstance(name, str) or not name.endswith('.md') or name.startswith('.')
            or name != name.strip() or len(name) > settings.notes.max_name_chars
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or not name[:-3].strip(' .')
            or name[:-3] != name[:-3].rstrip(' .')
            or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]', name.split('.')[0], re.I)):
        raise NoteError('文件名须为单层 .md 文件名，不能包含路径或 Windows 保留名称')
    return name


def _safe(root: Path, path: Path):
    # 拒绝符号链接与 Windows junction，包括工作区内部的链接。
    for p in (path, *path.parents):
        if p == root:
            break
        if p.is_symlink() or p.is_junction():
            raise NoteError('笔记路径不能包含链接')
    if not path.resolve().is_relative_to(root.resolve()):
        raise NoteError('笔记路径超出视频目录')
    if path.is_file() and path.stat().st_nlink > 1:
        raise NoteError('笔记路径不能包含硬链接')
    return path


def _root(video_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', video_id or ''):
        raise NoteError('视频标识无效')
    base = settings.videos_dir
    return _safe(base, artifacts.video_dir(video_id) / 'notes')


def _path(video_id, name):
    root = _root(video_id)
    return _safe(root, root / valid_name(name))


def _meta_path(video_id, name):
    root = _root(video_id)
    return _safe(root, root / '.history' / (valid_name(name) + '.json'))


def _revision(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _load(video_id, name):
    path = _path(video_id, name)
    if not path.is_file():
        raise NoteError('笔记不存在', 404)
    if path.stat().st_size > settings.notes.max_bytes:
        raise NoteError('笔记超过大小上限')
    text = path.read_bytes().decode('utf-8')
    revision = _revision(text)
    try:
        meta = artifacts.read_json(_meta_path(video_id, name))
    except (FileNotFoundError, ValueError):
        meta = {}
    if not isinstance(meta, dict) or meta.get('revision') != revision:
        meta = {}  # 外部改动或写入中断：不应用失配的历史/证据元信息。
    return text, revision, meta


def _evidence(video_id):
    try:
        evidence, _ = context.load_evidence(video_id)
        return 'sha256:' + outline_edits.fingerprint(evidence), id_index(evidence)
    except (FileNotFoundError, context.ContextError, ValueError, TypeError):
        return None, {}


def read(video_id, name):
    with lock(video_id):
        text, revision, meta = _load(video_id, name)
        version, index = _evidence(video_id)
        stale = not version or meta.get('evidence_version') != version
        resolved, _ = citations.resolve(citations.normalize(text), index, set(index)) if not stale else ([], [])
        from .. import pdfs
        pdf_resolved, _ = pdfs.resolve(text, video_id)
        resolved += pdf_resolved
        return {'name': name, 'text': text, 'revision': revision,
                'can_undo': bool(meta.get('previous')), 'citations': resolved, 'stale': stale}


def listing(video_id):
    with lock(video_id):
        root = _root(video_id)
        if not root.exists():
            return []
        result = []
        for path in sorted(root.glob('*.md'), key=lambda p: p.name.casefold()):
            text, revision, meta = _load(video_id, path.name)
            result.append({'name': path.name, 'revision': revision,
                           'agent_new': meta.get('created_by') == 'agent' and not meta.get('seen'),
                           'has_proposal': _proposal_path(video_id, path.name).is_file()})
        return result


def save(video_id, name, text, base_revision, *, evidence_version=None,
         author='user', label=None, sealed=False, created_by=None):
    with lock(video_id):
        path = _path(video_id, name)
        if not isinstance(text, str) or len(text.encode('utf-8')) > settings.notes.max_bytes:
            raise NoteError('笔记超过大小上限')
        previous = None
        carried = {}
        if path.exists():
            old, current, meta = _load(video_id, name)
            if base_revision != current:
                raise NoteError('笔记已被更新；请保留当前草稿并读取最新版本后合并', 409)
            if text == old:
                return read(video_id, name)
            previous = {'text': old, 'evidence_version': meta.get('evidence_version')}
            carried = {k: meta[k] for k in ('created_by', 'seen') if k in meta}
            if evidence_version is None:
                evidence_version = meta.get('evidence_version') or ''
        elif base_revision is not None:
            raise NoteError('笔记已不存在；请重新创建', 409)
        if evidence_version is None:
            evidence_version, _ = _evidence(video_id)
        meta = {'revision': _revision(text), 'evidence_version': evidence_version, 'previous': previous, **carried}
        if created_by and not carried:
            meta.update(created_by=created_by, seen=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
        try:
            tmp.write_bytes(text.encode('utf-8'))
            artifacts.write_json(_meta_path(video_id, name), meta)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        _record_version(video_id, name, text, meta['revision'], evidence_version,
                        author=author, label=label, sealed=sealed)
        return read(video_id, name)


def undo(video_id, name, base_revision):
    with lock(video_id):
        _, revision, meta = _load(video_id, name)
        if revision != base_revision:
            raise NoteError('笔记已被更新，无法撤销旧版本', 409)
        previous = meta.get('previous')
        if not previous:
            raise NoteError('没有可以撤销的修改', 409)
        # 空字符串也代表未知证据版本，避免把旧引用重新标为当前证据。
        return save(video_id, name, previous['text'], revision,
                    evidence_version=previous.get('evidence_version') or '',
                    label='撤销上次保存', sealed=True)


# ---------- 页面：标题转文件名、重命名、回收站 ----------

def name_from_title(video_id, title):
    """标题即文件名：去掉 Windows 非法字符，重名自动加序号。"""
    with lock(video_id):
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(title or ''))
        base = ' '.join(base.split()).strip(' .')
        if base.casefold().endswith('.md'):
            base = base[:-3].strip(' .')
        # 标题生成的文件名限 60 字：元信息与版本目录的路径更长，Windows 260 字符上限下留足余量。
        base = (base or '无标题')[:60].strip(' .') or '无标题'
        try:
            valid_name(base + '.md')
        except NoteError:
            base += ' 页'
        taken = {item['name'].casefold() for item in listing(video_id)}
        candidate, n = base + '.md', 2
        while candidate.casefold() in taken:
            candidate, n = f'{base} {n}.md', n + 1
        return valid_name(candidate)


def create(video_id, *, name=None, title=None, text=''):
    if name is None:
        name = name_from_title(video_id, title)
    return save(video_id, name, text or '', None, label='创建页面', sealed=not text)


def _dir_move(src: Path, dst: Path):
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)


def rename(video_id, name, new_name, base_revision):
    with lock(video_id):
        _, revision, _ = _load(video_id, name)
        if revision != base_revision:
            raise NoteError('笔记已被更新；请读取最新版本后再重命名', 409)
        new_name = valid_name(new_name)
        if new_name == name:
            return read(video_id, name)
        target = _path(video_id, new_name)
        if target.exists() and new_name.casefold() != name.casefold():
            raise NoteError('同名页面已存在', 409)
        _dir_move(_path(video_id, name), target)
        _dir_move(_meta_path(video_id, name), _meta_path(video_id, new_name))
        _dir_move(_versions_dir(video_id, name), _versions_dir(video_id, new_name))
        _dir_move(_proposal_path(video_id, name), _proposal_path(video_id, new_name))
        if _proposal_path(video_id, new_name).is_file():
            data = artifacts.read_json(_proposal_path(video_id, new_name))
            data['name'] = new_name
            artifacts.write_json(_proposal_path(video_id, new_name), data)
        return read(video_id, new_name)


def _trash_root(video_id):
    return _safe(_root(video_id), _root(video_id) / '.trash')


def _purge_trash(video_id):
    cutoff = time.time() - settings.notes.trash_days * 86400
    base = _trash_root(video_id)
    if not base.is_dir():
        return
    for item in base.iterdir():
        try:
            if item.is_dir() and not item.is_symlink() and artifacts.read_json(item / 'info.json')['ts'] < cutoff:
                shutil.rmtree(item)
        except (OSError, ValueError, KeyError):
            continue


def list_trash(video_id):
    with lock(video_id):
        base = _trash_root(video_id)
        items = []
        for entry in sorted(base.iterdir(), reverse=True) if base.exists() else []:
            if not re.fullmatch(r'\d+-[0-9a-f]{8}', entry.name):
                continue
            info = artifacts.read_json(_safe(_root(video_id), entry / 'info.json'))
            items.append({'trash_id': entry.name, 'name': valid_name(info['name']), 'deleted_at': info['ts']})
        return items


def delete(video_id, name):
    """移入回收站，返回 trash_id 供撤销。"""
    with lock(video_id):
        _load(video_id, name)
        _purge_trash(video_id)
        trash_id = f'{int(time.time() * 1000)}-{hashlib.sha256(name.encode()).hexdigest()[:8]}'
        folder = _safe(_root(video_id), _trash_root(video_id) / trash_id)
        folder.mkdir(parents=True, exist_ok=True)
        _dir_move(_path(video_id, name), folder / 'note.md')
        _dir_move(_meta_path(video_id, name), folder / 'meta.json')
        _dir_move(_versions_dir(video_id, name), folder / 'versions')
        _dir_move(_proposal_path(video_id, name), folder / 'proposal.json')
        artifacts.write_json(folder / 'info.json', {'name': name, 'ts': time.time()})
        return {'trash_id': trash_id, 'name': name}


def restore_trash(video_id, trash_id):
    with lock(video_id):
        if not re.fullmatch(r'\d+-[0-9a-f]{8}', trash_id or ''):
            raise NoteError('回收站标识无效')
        folder = _safe(_root(video_id), _trash_root(video_id) / trash_id)
        try:
            name = valid_name(artifacts.read_json(folder / 'info.json')['name'])
        except (OSError, ValueError, KeyError):
            raise NoteError('回收站里没有这个页面，可能已被清理', 404) from None
        if _path(video_id, name).exists():
            raise NoteError('同名页面已存在，请先重命名现有页面', 409)
        _dir_move(folder / 'note.md', _path(video_id, name))
        _dir_move(folder / 'meta.json', _meta_path(video_id, name))
        _dir_move(folder / 'versions', _versions_dir(video_id, name))
        _dir_move(folder / 'proposal.json', _proposal_path(video_id, name))
        shutil.rmtree(folder, ignore_errors=True)
        return read(video_id, name)


def mark_seen(video_id, name):
    with lock(video_id):
        _, _, meta = _load(video_id, name)
        if meta.get('created_by') and not meta.get('seen'):
            artifacts.write_json(_meta_path(video_id, name), {**meta, 'seen': True})
        return {'name': name}


# ---------- 版本历史 ----------

def _versions_dir(video_id, name):
    root = _root(video_id)
    return _safe(root, root / '.history' / (valid_name(name) + '.v'))


def _record_version(video_id, name, text, revision, evidence_version, *, author, label, sealed):
    directory = _versions_dir(video_id, name)
    directory.mkdir(parents=True, exist_ok=True)
    files = sorted(directory.glob('*.json'))
    now = time.time()
    if files and author == 'user' and not sealed:
        try:
            last = artifacts.read_json(files[-1])
            if last.get('author') == 'user' and not last.get('sealed') and now - last.get('ts', 0) < settings.notes.version_window_s:
                files.pop().unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    vid = f'{int(now * 1000):016d}-{revision[:8]}'
    artifacts.write_json(directory / f'{vid}.json', {
        'id': vid, 'ts': now, 'at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
        'author': author, 'label': label, 'sealed': bool(sealed or author != 'user'),
        'revision': revision, 'text': text, 'evidence_version': evidence_version})
    files = sorted(directory.glob('*.json'))
    for stale in files[:max(0, len(files) - settings.notes.max_versions)]:
        stale.unlink(missing_ok=True)


def versions(video_id, name):
    with lock(video_id):
        _, revision, _ = _load(video_id, name)
        directory = _versions_dir(video_id, name)
        items = []
        for file in sorted(directory.glob('*.json'), reverse=True) if directory.is_dir() else []:
            try:
                v = artifacts.read_json(file)
                items.append({'id': v['id'], 'at': v['at'], 'author': v['author'], 'label': v.get('label'),
                              'revision': v['revision'], 'current': v['revision'] == revision})
            except (OSError, ValueError, KeyError):
                continue
        return items


def read_version(video_id, name, version_id):
    with lock(video_id):
        _load(video_id, name)
        if not re.fullmatch(r'\d{16}-[0-9a-f]{8}', version_id or ''):
            raise NoteError('版本标识无效')
        try:
            v = artifacts.read_json(_safe(_root(video_id), _versions_dir(video_id, name) / f'{version_id}.json'))
        except (OSError, ValueError):
            raise NoteError('版本不存在', 404) from None
        return {'id': v['id'], 'at': v['at'], 'author': v['author'], 'label': v.get('label'),
                'revision': v['revision'], 'text': v['text'], 'evidence_version': v.get('evidence_version')}


def restore(video_id, name, version_id, base_revision):
    with lock(video_id):
        version = read_version(video_id, name, version_id)
        # 旧版本的证据版本原样带回：证据已变化的引用仍会被标为停用。
        return save(video_id, name, version['text'], base_revision, label='恢复旧版本', sealed=True,
                    evidence_version=version.get('evidence_version') or '')


# ---------- Agent 建议：不改原文，由用户逐条接受 ----------

def _proposal_path(video_id, name):
    root = _root(video_id)
    return _safe(root, root / '.proposals' / (valid_name(name) + '.json'))


def propose(video_id, name, text, base_revision, evidence_version):
    with lock(video_id):
        old, current, _ = _load(video_id, name)
        if base_revision != current:
            raise NoteError('笔记已被更新；请重新读取最新版本后再提出修改', 409)
        if not isinstance(text, str) or len(text.encode('utf-8')) > settings.notes.max_bytes:
            raise NoteError('笔记超过大小上限')
        if text == old:
            return None
        data = {'id': uuid.uuid4().hex, 'name': name, 'base_revision': current, 'base_text': old, 'text': text,
                'evidence_version': evidence_version, 'created_at': datetime.now(timezone.utc).isoformat()}
        artifacts.write_json(_proposal_path(video_id, name), data)
        return data


def proposal(video_id, name):
    with lock(video_id):
        _load(video_id, name)
        try:
            return artifacts.read_json(_proposal_path(video_id, name))
        except (FileNotFoundError, ValueError):
            return None


def discard_proposal(video_id, name):
    with lock(video_id):
        _proposal_path(video_id, name).unlink(missing_ok=True)
        return {'name': name}


def resolve_proposal(video_id, name, proposal_id, text, base_revision, accepted, total):
    """text 是用户在当前正文上应用了所选修改后的结果；一条都不接受时直接丢弃建议。"""
    with lock(video_id):
        data = proposal(video_id, name)
        if not data or data['id'] != proposal_id:
            raise NoteError('建议已被替换或不存在，请刷新页面', 409)
        if accepted <= 0:
            return _finish_proposal(video_id, name)
        version, index = _evidence(video_id)
        if not version or version != data['evidence_version']:
            raise NoteError('课程证据已变化，这份建议已失效，请放弃后重新让 Agent 修改', 409)
        _, invalid = citations.resolve(citations.normalize(text), index, set(index))
        from .. import pdfs
        _, pdf_invalid = pdfs.resolve(text, video_id)
        invalid += pdf_invalid
        if invalid:
            raise NoteError('笔记引用不存在：' + ', '.join(invalid))
        note = save(video_id, name, text, base_revision, evidence_version=data['evidence_version'],
                    author='agent', label=f'接受 Agent 建议（{accepted}/{total} 处）', sealed=True)
        discard_proposal(video_id, name)
        return note


def _finish_proposal(video_id, name):
    discard_proposal(video_id, name)
    return read(video_id, name)


def import_legacy(video_id, conversation_id, old_root):
    """复制旧对话笔记；确定性目标名保证重试不会覆盖用户修改。"""
    source = _safe(old_root, old_root / 'notes')
    if not source.exists():
        return
    with lock(video_id):
        for file in source.rglob('*.md'):
            _safe(old_root, file)
            if not file.is_file():
                continue
            key = conversation_id + '/' + file.relative_to(source).as_posix()
            suffix = hashlib.sha256(key.encode()).hexdigest()[:10]
            name = f'旧对话-{conversation_id[:8]}-{file.stem[:50]}-{suffix}.md'
            if not _path(video_id, name).exists():
                if file.stat().st_size > settings.notes.max_bytes:
                    raise NoteError('旧笔记超过大小上限，原文件已保留')
                save(video_id, name, file.read_text(encoding='utf-8'), None, evidence_version='',
                     label='导入旧对话笔记', sealed=True)


def import_all_legacy(video_id):
    from .. import db
    from ..codex import settings as codex_settings
    from .workspace import _workspace_root
    options = codex_settings.get()
    rows = db.fetch_all('SELECT id FROM chat_conversations WHERE video_id=?', (video_id,))
    current = db.get_setting('codex_conversation:' + video_id, {}) or {}
    for row in rows:
        cid = row['id']
        state = current if current.get('conversation_id') == cid else db.get_setting(f'codex_saved:{video_id}:{cid}', {}) or {}
        roots = {*state.get('notes_roots', []), options['workspace_root'], state.get('workspace_root') or options['workspace_root']}
        for root in roots:
            import_legacy(video_id, cid, _workspace_root(video_id, cid, {**options, 'workspace_root': root}))
