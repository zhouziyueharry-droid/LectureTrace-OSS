import copy

import pytest

from app.package_validation import validate_json


def wrapped(data):
    return {'schema_version': 1, 'data': data}


@pytest.mark.parametrize('name,value', [
    ('outline.json', wrapped({'chapters': [None]})),
    ('outline.json', wrapped({'chapters': [{'children': [1]}]})),
    ('outline.json', wrapped({'chapters': [], 'admin': None})),
    ('outline.json', wrapped({'chapters': [{'evidence': [{}]}]})),
    ('outline.json', wrapped({'chapters': [{'start': float('nan')}]})),
    ('evidence.json', wrapped({'pages': [None], 'sections': [], 'speech': []})),
    ('evidence.json', wrapped({'pages': [{'id': [], 'jump': 0}], 'sections': [], 'speech': []})),
    ('segments.json', wrapped({'sections': [False], 'speech': []})),
    ('cues.json', wrapped([{'id': 'C0001', 'start': 0, 'end': 1, 'text': {}}])),
    ('keyframes.json', wrapped([{'id': 'S001', 'appearances': [[0]], 'image': 'keyframes/S001.jpg'}])),
    ('keyframes.json', wrapped([{'id': 'S001', 'appearances': [], 'image': 'source.mp4'}])),
    ('slides.json', wrapped([{'id': 'S001', 'occurrences': [None]}])),
    ('slides.json', wrapped([{'id': 'S001', 'speech_categories': 7}])),
    ('notes/.history/a.md.json', []),
    ('notes/.history/a.md.json', {'previous': {'text': []}}),
    ('notes/.history/a.md.v/0000000000000001-abcdef12.json', {'id': 'x'}),
    ('notes/.proposals/a.md.json', {'id': 'x'}),
    ('notes/.trash/1-abcdef12/info.json', {'name': '../a.md', 'ts': 1}),
    ('notes/.trash/1-abcdef12/info.json', {'name': 'a.md', 'ts': 'yesterday'}),
    ('stage_meta.json', {'asr': []}),
])
def test_malformed_reader_shapes_rejected(name, value):
    with pytest.raises(ValueError):
        validate_json(name, value, {'keyframes/S001.jpg', 'source.mp4'})


@pytest.mark.parametrize('name,value', [
    ('outline.json', wrapped({'chapters': [{'title': 'Old outline', 'summary': '', 'evidence': ['S001'],
                                           'children': [{'title': 'Child', 'start': 0}]}]})),
    ('slides.json', wrapped({'slides': [{'id': 'S001', 'speech_categories': {'T0001': 'lecture'}}]})),
    ('slides.json', wrapped([{'id': 'S001', 'speech_categories': [{'id': 'T0001', 'category': 'lecture'}]}])),
    ('keyframes.json', wrapped([{'id': 'S001', 'image': 'keyframes/S001.jpg', 'appearances': [[0, 1]],
                                 'similar_prev': {'id': 'S000', 'score': 0.6}}])),
    ('evidence.json', wrapped({'pages': [{'id': 'S001', 'jump': 0, 'sections': ['S001.1']}],
                               'sections': [], 'speech': []})),
    ('notes/.history/a.md.json', {'revision': 'old', 'evidence_version': None,
                                'previous': {'text': 'previous', 'evidence_version': ''}}),
    ('notes/.trash/1-abcdef12/info.json', {'name': 'a.md', 'ts': 1}),
])
def test_legacy_shapes_preserved_without_mutation(name, value):
    before = copy.deepcopy(value)
    validate_json(name, value, {'keyframes/S001.jpg'})
    assert value == before
