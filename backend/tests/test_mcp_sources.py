"""Source-only tools must work before translation, slide reading or evidence exists."""
import asyncio
import dataclasses
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip('mcp')
from mcp.server.fastmcp.exceptions import ToolError
from app import artifacts, config, db
from app.mcp_server import create_server
from test_mcp import data, snapshot

VID = '123456abcdef'


@pytest.fixture
def source_only(tmp_settings):
    db.execute('INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)',
               (VID, 'Source only', 'example.mp4', db.now_iso(), 'awaiting_confirm'))
    artifacts.write_artifact(VID, 'transcript.json', {'language': 'en', 'segments': [
        {'start': 0.125, 'end': 2.5, 'text': 'First original sentence.',
         'words': [{'word': 'NOT IN OUTPUT', 'start': 0.125, 'end': 1}]},
        {'start': 9, 'end': 12, 'text': 'Second original sentence.'}]}, {})
    root = artifacts.video_dir(VID)
    (root / 'keyframes').mkdir()
    Image.new('RGB', (12, 12), 'red').save(root / 'keyframes/first.jpg')
    Image.new('RGB', (12, 12), 'blue').save(root / 'keyframes/revisit.jpg')
    artifacts.write_artifact(VID, 'keyframes.json', [
        {'id': 'S001', 'image': 'keyframes/first.jpg', 'appearances': [[0, 5], [10, 15]],
         'appearance_images': ['keyframes/first.jpg', 'keyframes/revisit.jpg']},
        {'id': 'S002', 'image': 'keyframes/first.jpg', 'appearances': [[12, 14]]}], {})
    return root


def call(name, **kwargs):
    return asyncio.run(create_server().call_tool(name, {'video_id': VID, **kwargs}))


def test_full_transcript_without_other_artifacts(source_only):
    before = snapshot(config.settings.data_dir)
    result = data(call('read_video_transcript'))
    assert result['text'] == '[0.125-2.500] First original sentence.\n[9.000-12.000] Second original sentence.'
    assert result['language'] == 'en' and result['segment_count'] == 2
    assert result['source'] == 'asr_transcript'
    assert result['complete'] and result['next_text_offset'] is None
    assert result['total_chars'] == len(result['text'])
    assert 'NOT IN OUTPUT' not in str(result)
    assert snapshot(config.settings.data_dir) == before


def test_full_transcript_pagination_and_revision(source_only, set_settings):
    full = data(call('read_video_transcript'))
    set_settings(dataclasses.replace(config.settings, mcp=dataclasses.replace(
        config.settings.mcp, transcript_chunk_chars=17)))
    first = data(call('read_video_transcript'))
    assert not first['complete'] and first['next_text_offset'] == 17
    text = first['text']
    offset = first['next_text_offset']
    while offset is not None:
        part = data(call('read_video_transcript', text_offset=offset, version=first['version']))
        assert not part['complete']
        text += part['text']
        offset = part['next_text_offset']
    assert text == full['text']
    assert call('read_video_transcript', text_offset=17).isError
    assert call('read_video_transcript', text_offset=9999, version=first['version']).isError
    obj = artifacts.read_json(source_only / 'transcript.json')
    obj['data']['segments'][0]['text'] = 'Changed.'
    artifacts.write_json(source_only / 'transcript.json', obj)
    assert call('read_video_transcript', text_offset=17, version=first['version']).isError


def test_keyframes_by_time_revisits_gaps_and_overlap(source_only):
    before = snapshot(config.settings.data_dir)
    first = call('view_keyframe', timestamp=0)
    revisit = call('view_keyframe', timestamp=10)
    assert data(first)['matches'][0]['appearance_index'] == 1
    assert data(revisit)['matches'][0]['appearance_index'] == 2
    assert first.content[1].data != revisit.content[1].data
    assert revisit.content[1].mimeType == 'image/jpeg'
    gap = call('view_keyframe', timestamp=5)
    assert data(gap)['matches'] == [] and len(gap.content) == 1
    assert data(call('view_keyframe', timestamp=15))['matches'] == []
    overlap = data(call('view_keyframe', timestamp=12))
    assert overlap['total_matches'] == 2 and overlap['next_offset'] == 1
    second = data(call('view_keyframe', timestamp=12, offset=1, version=overlap['version']))
    assert second['matches'][0]['page_id'] == 'S002'
    assert second['matches'][0]['image_role'] == 'page_representative'
    assert second['next_offset'] is None
    assert call('view_keyframe', timestamp=12, offset=1).isError
    assert snapshot(config.settings.data_dir) == before


@pytest.mark.parametrize('name,args,file', [
    ('read_video_transcript', {}, 'transcript.json'),
    ('view_keyframe', {'timestamp': 1}, 'keyframes.json'),
])
def test_missing_and_corrupt_sources(source_only, name, args, file):
    path = source_only / file
    path.unlink()
    assert call(name, **args).isError
    path.write_text('not json', encoding='utf-8')
    result = call(name, **args)
    assert result.isError and str(source_only) not in result.content[0].text


def test_keyframe_path_escape_and_version(source_only, tmp_path):
    first = data(call('view_keyframe', timestamp=1))
    obj = artifacts.read_json(source_only / 'keyframes.json')
    obj['data'][0]['appearance_images'][0] = str(tmp_path / 'outside.jpg')
    Image.new('RGB', (12, 12)).save(tmp_path / 'outside.jpg')
    artifacts.write_json(source_only / 'keyframes.json', obj)
    assert call('view_keyframe', timestamp=1, version=first['version']).isError
    result = call('view_keyframe', timestamp=1)
    assert result.isError and str(tmp_path) not in result.content[0].text


@pytest.mark.parametrize('args', [{'timestamp': -1}, {'timestamp': float('inf')}, {'timestamp': 1, 'offset': -1}])
def test_keyframe_bad_parameters(source_only, args):
    with pytest.raises(ToolError):
        call('view_keyframe', **args)


def test_transcript_default_budget_and_empty_source(source_only):
    text = 'Original English sentence. ' * 2500
    artifacts.write_artifact(VID, 'transcript.json', {'language': 'en', 'segments': [
        {'start': 0, 'end': 100, 'text': text}]}, {})
    full = data(call('read_video_transcript'))
    assert 24000 < len(full['text']) < 160000
    assert full['complete'] and full['text'].endswith(text.strip())
    artifacts.write_artifact(VID, 'transcript.json', {'language': 'zh', 'segments': []}, {})
    empty = data(call('read_video_transcript'))
    assert empty['complete'] and empty['text'] == '' and empty['language'] == 'zh'


@pytest.mark.parametrize('segment', [
    {'start': -1, 'end': 2, 'text': 'bad'},
    {'start': 3, 'end': 2, 'text': 'bad'},
    {'start': 0, 'end': float('nan'), 'text': 'bad'},
    {'start': True, 'end': 2, 'text': 'bad'},
    {'start': 0, 'end': 2, 'text': None},
])
def test_invalid_transcript_segments(source_only, segment):
    artifacts.write_artifact(VID, 'transcript.json', {'language': 'en', 'segments': [segment]}, {})
    assert call('read_video_transcript').isError


def test_source_resolved_path_escape(source_only, tmp_path, monkeypatch):
    outside = tmp_path / 'outside.json'
    resolve = Path.resolve
    # Simulate a source symlink resolving outside the library, without OS symlink privileges.
    monkeypatch.setattr(Path, 'resolve', lambda p, *a, **kw:
                        outside if p == source_only / 'transcript.json' else resolve(p, *a, **kw))
    result = call('read_video_transcript')
    assert result.isError and str(tmp_path) not in result.content[0].text
