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
