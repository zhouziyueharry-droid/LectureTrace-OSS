import pytest

from app import artifacts
from app.qa.evidence_reader import EvidenceReader, WorkspaceError
from test_codex_workspace import VID, course


def reader(course):
    return EvidenceReader(VID, *course)


def test_time_returns_summary_not_transcript_and_respects_gaps(course):
    r = reader(course)
    units = r.units_at_time(30)
    assert [u['unit_id'] for u in units] == ['S001.1']
    assert units[0]['summary'] == 'Why normalize each mini batch.'
    assert units[0]['speech_count'] == 2
    assert 'Gamma restores' not in str(units)
    assert r.units_at_time(95) == []
    assert r.units_at_time(100)[0]['unit_id'] == 'S002.1'
    assert r.units_at_time(150) == []
    assert r.units_at_time(1)[0]['summary_status'] == 'missing'


def test_unit_reads_exact_members_not_numeric_range(course):
    evidence = course[0]
    evidence['sections'][1]['t_ids'] = ['T0002', 'T0004']
    artifacts.write_json(artifacts.video_dir(VID) / 'evidence.json', {'data': evidence})
    result = reader(course).read_unit('S001.1')
    assert '[T0002]' in result['text'] and '[T0004]' in result['text']
    assert '[T0003]' not in result['text']
    assert 'Why normalize' not in result['text']
    with pytest.raises(WorkspaceError):
        reader(course).read_unit('S001')


def test_speech_overlap_repeated_page_and_summary_truncation(course, monkeypatch):
    evidence = course[0]
    first = evidence['sections'][1]
    first.update(speech_start=9, speech_end=105, truncated=True)
    evidence['sections'].append({**first, 'id': 'S001.2', 'jump': 160,
                                 'display_start': 160, 'display_end': 180,
                                 'speech_start': 160, 'speech_end': 180})
    artifacts.write_json(artifacts.video_dir(VID) / 'evidence.json', {'data': evidence})
    r = reader(course)
    assert {u['unit_id'] for u in r.units_at_time(102)} == {'S001.1', 'S002.1'}
    assert r.units_at_time(170)[0]['unit_id'] == 'S001.2'
    assert r.unit_preview('S001.1')['summary_status'] == 'partial'
    for t in (-1, float('nan'), float('inf')):
        with pytest.raises(WorkspaceError):
            r.units_at_time(t)


def test_unit_read_rejects_stale_and_missing_members(course):
    r = reader(course)
    evidence = course[0]
    evidence['sections'][1]['t_ids'].append('T9999')
    artifacts.write_json(artifacts.video_dir(VID) / 'evidence.json', {'data': evidence})
    with pytest.raises(WorkspaceError):
        r.units_at_time(20)
    with pytest.raises(WorkspaceError):
        reader(course).read_unit('S001.1')
