"""Validate portable JSON at the shapes used by readers, without rewriting it."""
import math
import re

from .qa import notes


def _check(ok):
    if not ok:
        raise ValueError('课程包 JSON 内容结构无效')


def _object(value):
    _check(isinstance(value, dict))
    return value


def _rows(value):
    _check(isinstance(value, list))
    for row in value:
        _object(row)
    return value


def _strings(value):
    _check(isinstance(value, list) and all(isinstance(x, str) for x in value))


def _number(value):
    _check(type(value) in (float, int) and math.isfinite(value) and value >= 0)


def _fields(row, files):
    """Optional fields remain optional for legacy data; present fields must be usable."""
    _object(row)
    for key in ('id', 'title', 'summary', 'text', 'zh', 'visual_content', 'spoken_summary',
                'page', 'section', 'cue_first', 'cue_last', 'category',
                'node_id', 'revision', 'language', 'status'):
        if key in row and row[key] is not None:
            _check(isinstance(row[key], str))
    for key in ('start', 'end', 'jump', 'display_start', 'display_end', 'speech_start',
                'speech_end', 'duration_s'):
        if key in row and row[key] is not None:
            _number(row[key])
    for key in ('evidence', 't_ids', 'local_tags', 'truncated_t_ids', 'sections', 'warnings'):
        if key in row:
            _strings(row[key])
    if row.get('similar_prev') is not None:
        previous = _object(row['similar_prev'])
        _check(isinstance(previous.get('id'), str))
        _number(previous.get('score'))
    for key in ('image', 'appearance_images'):
        if key not in row or row[key] is None:
            continue
        images = row[key] if key == 'appearance_images' else [row[key]]
        _strings(images)
        for image in images:
            _check(bool(re.fullmatch(r'keyframes/S\d{3,6}(?:_\d+)?\.jpg', image)) and image in files)


def _outline(value, files):
    _object(value)
    _fields(value, files)
    seen = set()

    def node(row, depth):
        _fields(row, files)
        # A missing node_id is migrated by outline_edits.ensure for old outlines.
        if row.get('node_id') is not None:
            _check(bool(row['node_id']) and row['node_id'] not in seen)
            seen.add(row['node_id'])
        children = _rows(row.get('children', []))
        _check(not children or depth == 0)
        for child in children:
            node(child, depth + 1)

    for chapter in _rows(value.get('chapters')):
        node(chapter, 0)
    if 'admin' in value:
        admin = _object(value['admin'])
        _fields(admin, files)
        for item in _rows(admin.get('items', [])):
            node(item, 1)


def _note(name, value):
    row = _object(value)
    for key in ('revision', 'evidence_version', 'created_by', 'id', 'at', 'author', 'label',
                'name', 'base_revision', 'base_text', 'text', 'created_at'):
        if key in row and row[key] is not None:
            _check(isinstance(row[key], str))
    for key in ('ts',):
        if key in row:
            _number(row[key])
    for key in ('sealed', 'seen'):
        if key in row:
            _check(type(row[key]) is bool)
    if row.get('previous') is not None:
        prev = _object(row['previous'])
        _check(isinstance(prev.get('text'), str))
        _check(prev.get('evidence_version') is None or isinstance(prev['evidence_version'], str))
    if name.endswith('/info.json'):
        notes.valid_name(row.get('name'))
        _number(row.get('ts'))
    elif '/.proposals/' in name or name.endswith('/proposal.json'):
        for key in ('id', 'name', 'base_revision', 'base_text', 'text', 'created_at'):
            _check(isinstance(row.get(key), str))
        notes.valid_name(row['name'])
        _check('evidence_version' in row)
    elif '/versions/' in name or '.md.v/' in name:
        for key in ('id', 'at', 'author', 'revision', 'text'):
            _check(isinstance(row.get(key), str))
        _check(bool(re.fullmatch(r'\d{16}-[0-9a-f]{8}', row['id'])))
        _number(row.get('ts'))


def validate_json(name: str, value, files: set[str]):
    """Raise ValueError for malformed structures; keep valid artifact bytes intact."""
    if name.startswith('notes/'):
        _note(name, value)
        return
    if name == 'stage_meta.json':
        for metadata in _object(value).values():
            _object(metadata)
        return
    data = _object(value).get('data')
    if name == 'outline.json':
        _outline(data, files)
        for history in _rows(value.get('outline_history', [])):
            _outline(history.get('outline'), files)
    elif name in ('evidence.json', 'segments.json'):
        _object(data)
        keys = ('pages', 'sections', 'speech') if name == 'evidence.json' else ('sections', 'speech')
        ids = set()
        for key in keys:
            for row in _rows(data.get(key)):
                _fields(row, files)
                _check(isinstance(row.get('id'), str) and bool(row['id']) and row['id'] not in ids)
                ids.add(row['id'])
                if name == 'evidence.json':
                    _number(row.get('jump'))
                if key == 'speech':
                    _number(row.get('start'))
                    _number(row.get('end'))
    elif name in ('cues.json', 'keyframes.json', 'slides.json', 'transcript.json'):
        if name == 'transcript.json':
            _fields(_object(data), files)
            data = data.get('segments')
        elif name == 'slides.json' and isinstance(data, dict):
            data = data.get('slides')
        for row in _rows(data):
            _fields(row, files)
            if name != 'transcript.json':
                _check(isinstance(row.get('id'), str) and bool(row['id']))
            if name in ('cues.json', 'transcript.json'):
                _number(row.get('start'))
                _number(row.get('end'))
                _check(isinstance(row.get('text'), str))
            if name == 'keyframes.json':
                _check(isinstance(row.get('appearances'), list))
                for pair in row['appearances']:
                    _check(isinstance(pair, list) and len(pair) == 2)
                    for n in pair:
                        _number(n)
            if 'occurrences' in row:
                for occurrence in _rows(row['occurrences']):
                    _fields(occurrence, files)
            if 'speech_categories' in row:
                categories = row['speech_categories']
                if isinstance(categories, dict):
                    _check(all(isinstance(v, str) for v in categories.values()))
                else:
                    for category in _rows(categories):
                        _fields(category, files)
    elif name == 'audio.json':
        _fields(_object(data), files)
        _number(data.get('duration_s'))
