"""问答运行设置；与流水线的 API 模型设置独立。"""
from __future__ import annotations

import os
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from .. import config, db

KEY = 'codex_qa_settings'
RETIRED_LIMITS = {'max_tool_calls', 'max_read_tokens', 'max_images'}


def application_dir() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'LectureTrace'


def get() -> dict:
    defaults = {**asdict(config.settings.codex_qa), 'provider': 'api', 'model': 'gpt-6-sol',
                'effort': 'medium', 'workspace_root': str(application_dir() / 'workspaces')}
    stored = db.get_setting(KEY, {}) or {}
    return {k: v for k, v in {**defaults, **stored}.items() if k not in RETIRED_LIMITS}


def update(values: dict) -> dict:
    # Older open tabs may still send these fields; they no longer affect execution.
    values = {k: v for k, v in values.items() if k not in RETIRED_LIMITS}
    current = get()
    if set(values) - set(current):
        raise ValueError('未知的 Codex 设置')
    candidate = {**current, **{k: v for k, v in values.items() if v is not None}}
    if candidate['provider'] not in ('api', 'codex', 'stepwise_qa'):
        raise ValueError('问答运行方式无效')
    if candidate['effort'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ValueError('思考强度无效')
    model = candidate['model']
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise ValueError('模型名称无效')
    for key in ('search_k', 'snippet_chars'):
        value = candidate[key]
        minimum = 1
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= 100000:
            raise ValueError(f'{key} 需要是 {minimum}–100000 的整数')
    if not isinstance(candidate['workspace_root'], str) or not candidate['workspace_root'].strip():
        raise ValueError('工作区根目录需要是绝对路径文本')
    root = Path(candidate['workspace_root']).expanduser()
    if not root.is_absolute():
        raise ValueError('工作区根目录需要是绝对路径')
    root = root.resolve()
    if root.is_relative_to(config.ROOT_DIR.resolve()) or any((p / '.git').exists() for p in (root, *root.parents)):
        raise ValueError('课程工作区必须放在项目目录之外')
    if root == Path(root.anchor) or root == application_dir().resolve():
        raise ValueError('请使用独立的课程工作区目录')
    candidate['workspace_root'] = str(root)
    candidate['model'] = model.strip()
    with db.connect() as conn:
        if any(candidate[key] != current[key] for key in ('workspace_root', 'provider')):
            for row in conn.execute('SELECT key, value FROM app_settings WHERE key GLOB ? OR key GLOB ?',
                                    ('codex_conversation:*', 'codex_saved:*')).fetchall():
                state = json.loads(row['value'])
                if current['workspace_root'] != candidate['workspace_root'] and state.get('conversation_id'):
                    _copy_notes(current['workspace_root'], root, row['key'].split(':')[1],
                                state['conversation_id'])
                    state['notes_roots'] = list(dict.fromkeys([
                        *state.get('notes_roots', []), str(Path(current['workspace_root']).resolve()), str(root)]))
                state.pop('thread_id', None)
                state.pop('fingerprint', None)
                conn.execute('UPDATE app_settings SET value = ? WHERE key = ?',
                             (json.dumps(state), row['key']))
        conn.execute('INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
                     (KEY, json.dumps(candidate)))
    return candidate


def _copy_notes(previous_root, root, video_id, conversation_id):
    if not all(isinstance(part, str) and re.fullmatch(r'[A-Za-z0-9_-]+', part)
               for part in (video_id, conversation_id)):
        raise ValueError('已保存的课程对话标识无效')
    previous_root = Path(previous_root).resolve()
    source = previous_root / video_id / conversation_id / 'notes'
    target = root / video_id / conversation_id / 'notes'
    if source.resolve() == target.resolve():
        return
    if target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(target.resolve()):
        raise ValueError('新旧笔记目录不能相互嵌套，设置未保存')
    for folder, boundary in ((source, previous_root), (target, root)):
        if not folder.resolve().is_relative_to(boundary):
            raise ValueError('笔记路径超出工作区，设置未保存')
        if folder.exists() and any(path.is_symlink() or not path.resolve().is_relative_to(folder.resolve())
                                   for path in (folder, *folder.rglob('*'))):
            raise ValueError('笔记目录包含链接，设置未保存')
    if source.is_dir():
        try:
            shutil.copytree(source, target, dirs_exist_ok=True)
        except OSError as error:
            raise ValueError('复制会话笔记失败，设置未保存；原笔记仍在原目录') from error
