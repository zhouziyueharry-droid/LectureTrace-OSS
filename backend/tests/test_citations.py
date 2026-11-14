from app.qa import citations

EV = {"S012": {"jump": 750.0}, "T0142": {"jump": 4260.5}, "S012.2": {"jump": 900.0}, "N003": {"jump": 1200.0}}


def test_extract_and_resolve():
    text = "BN 归一化激活 [S012]，见讲解 [T0142]。再次 [S012]。编造 [S999] 与 [T0001]。"
    assert citations.extract_ids(text) == ["S012", "T0142", "S999", "T0001"]
    valid, invalid = citations.resolve(text, EV)
    assert valid == [{"id": "S012", "start": 750.0}, {"id": "T0142", "start": 4260.5}]
    assert invalid == ["S999", "T0001"]


def test_four_id_kinds_and_start():
    text = "页 [S012]、出现 [S012.2]、无页段 [N003]、讲解 [T0142]；不存在的出现 [S012.9]"
    assert citations.extract_ids(text) == ["S012", "S012.2", "N003", "T0142", "S012.9"]
    valid, invalid = citations.resolve(text, EV)
    assert valid == [{"id": "S012", "start": 750.0}, {"id": "S012.2", "start": 900.0},
                     {"id": "N003", "start": 1200.0}, {"id": "T0142", "start": 4260.5}]
    assert invalid == ["S012.9"]


def test_resolve_with_allowed_set():
    valid, invalid = citations.resolve("[S012][T0142]", EV, allowed={"S012"})
    assert valid == [{"id": "S012", "start": 750.0}] and invalid == ["T0142"]


def test_grouped_citations_normalized():
    text = "结论 [S012, T0142] 以及 [S012，T0142] 和 [S012.2、N003]"
    assert citations.extract_ids(text) == ["S012", "T0142", "S012.2", "N003"]
    assert citations.normalize(text) == "结论 [S012][T0142] 以及 [S012][T0142] 和 [S012.2][N003]"


def test_ignores_non_evidence_brackets():
    text = "数组 [1, 2]、链接 [text](url)、[S12] 位数不够、[C0001] 字幕 ID、[N01]、[S012.] 残缺"
    assert citations.extract_ids(text) == []
    assert citations.normalize(text) == text


def test_transcript_ranges_expand_in_order_and_validate_every_member():
    text = '来源 [T0152-T0154, S012][T0153] 与 [ T0078 – T0079 ]'
    expected = ['T0152', 'T0153', 'T0154', 'S012', 'T0078', 'T0079']
    assert citations.extract_ids(text) == expected
    assert citations.normalize(text) == '来源 [T0152][T0153][T0154][S012][T0153] 与 [T0078][T0079]'
    index = {key: {'jump': n * 10} for n, key in enumerate(expected)}
    valid, invalid = citations.resolve(text, index, allowed=set(expected) - {'T0153'})
    assert invalid == ['T0153']
    assert [c['id'] for c in valid] == [key for key in expected if key != 'T0153']
    del index['T0153']
    assert citations.resolve('[T0152—T0154]', index)[1] == ['T0153']


def test_invalid_or_excessive_ranges_are_not_silently_accepted():
    for marker in ('T0004-T0001', 'T0001-T99999999999999999999', 'T0001-T1001'):
        text = f'[{marker}]'
        assert citations.normalize(text) == text
        assert citations.resolve(text, {}) == ([], [marker])
    assert citations.normalize('[T9999-T10000]') == '[T9999][T10000]'
    assert citations.normalize('[T0152-T0152]') == '[T0152]'
