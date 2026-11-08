import base64
import io

import cv2
import numpy as np
import pytest
from PIL import Image

from app import artifacts
from app.qa.evidence_reader import EvidenceReader, WorkspaceError
from app.qa import slide_locator
from test_codex_workspace import VID, course


def slide(seed=1):
    rng = np.random.default_rng(seed)
    image = np.full((480, 640, 3), 255, np.uint8)
    cv2.putText(image, 'Lecture / same template', (25, 40), cv2.FONT_HERSHEY_SIMPLEX, .8, (20, 20, 20), 2)
    for i in range(18):
        x, y = int(rng.integers(35, 580)), int(rng.integers(90, 430))
        cv2.circle(image, (x, y), int(rng.integers(5, 25)), (30, 30, 30), 2)
        cv2.putText(image, f'{seed}:{i}', (x, y), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 0), 1)
    return image


def encoded(image):
    return base64.b64encode(cv2.imencode('.png', image)[1]).decode('ascii')


@pytest.fixture
def pictures(course):
    folder = artifacts.video_dir(VID) / 'keyframes'
    cv2.imwrite(str(folder / 'S001.jpg'), slide())
    cv2.imwrite(str(folder / 'S002.jpg'), slide(9))
    first = course[0]['sections'][1]
    course[0]['sections'].append({**first, 'id': 'S001.2', 'jump': 200, 'display_start': 200, 'display_end': 250})
    course[0]['pages'][0]['sections'].append('S001.2')
    artifacts.write_json(artifacts.video_dir(VID) / 'evidence.json', {'data': course[0]})
    return EvidenceReader(VID, *course)


def test_locates_scaled_perspective_annotated_page_and_all_occurrences(pictures):
    image = slide()
    cv2.line(image, (40, 270), (440, 280), (0, 0, 200), 3)
    matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [639, 0], [639, 479], [0, 479]]),
                                       np.float32([[30, 20], [590, 0], [620, 450], [0, 470]]))
    query = cv2.warpPerspective(image, matrix, (640, 480), borderValue=(255, 255, 255))
    result = slide_locator.locate(pictures, encoded(query))
    assert result['matches'][0]['page_id'] == 'S001'
    assert {r['unit_id'] for r in result['matches']} == {'S001.1', 'S001.2'}
    assert result['matches'][0]['timestamp'] == 10
    assert 'spoken_summary' not in str(result) and 'Gamma restores' not in str(result)


def test_blank_and_unrelated_page_are_not_forced_matches(pictures):
    assert slide_locator.locate(pictures, encoded(np.full((480, 640, 3), 255, np.uint8)))['matches'] == []
    assert slide_locator.locate(pictures, encoded(slide(222)))['matches'] == []


def test_pagination_and_invalid_input(pictures):
    first = slide_locator.locate(pictures, encoded(slide()), offset=0, size=1)
    second = slide_locator.locate(pictures, encoded(slide()), offset=first['next_offset'], size=1)
    assert first['next_offset'] == 1 and second['next_offset'] is None
    assert first['matches'][0]['unit_id'] != second['matches'][0]['unit_id']
    for value in ('not base64', base64.b64encode(b'not an image').decode(), 'https://example.com/x.jpg'):
        with pytest.raises(WorkspaceError):
            slide_locator.locate(pictures, value)


def test_image_path_escape_and_missing_images(pictures, tmp_path):
    pictures.pages['S001']['image'] = str(tmp_path / 'private.png')
    with pytest.raises(WorkspaceError):
        slide_locator.locate(pictures, encoded(slide()))


def test_query_limits_before_allocation(monkeypatch):
    import dataclasses
    from app import config
    monkeypatch.setattr(config, 'settings', dataclasses.replace(config.settings,
        slide_match=dataclasses.replace(config.settings.slide_match, max_image_bytes=100, max_image_pixels=100)))
    with pytest.raises(WorkspaceError):
        slide_locator.decode_image(b'x' * 101)
    out = io.BytesIO()
    Image.new('L', (20, 20)).save(out, format='PNG')
    with pytest.raises(WorkspaceError):
        slide_locator.decode_image(out.getvalue())
