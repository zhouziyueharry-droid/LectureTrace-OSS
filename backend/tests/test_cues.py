"""cues 切分：用合成词序列，不依赖真实音频。"""
from app.pipeline.cues import join_words, split_cues, words_from_transcript


def make_words(text: str, start: float = 0.0, step: float = 0.3, gaps: dict[int, float] | None = None):
    """把文本按空格拆成词，每词 step 秒；gaps={词序号: 该词前额外停顿秒数}。"""
    words, t = [], start
    for i, w in enumerate(text.split()):
        t += (gaps or {}).get(i, 0.0)
        words.append({"start": round(t, 3), "end": round(t + step * 0.9, 3), "word": " " + w})
        t += step
    return words


def check_invariants(cues, max_chars=84, max_s=7.0):
    for i, c in enumerate(cues, 1):
        assert c["id"] == f"C{i:04d}"
        assert c["zh"] is None
        assert c["end"] > c["start"]
        assert c["text"] == c["text"].strip() and "  " not in c["text"]
    for a, b in zip(cues, cues[1:]):
        assert a["end"] <= b["start"], "相邻条目不能重叠"


def test_sentence_end_splits():
    cues = split_cues(make_words("Hello everyone. Today we talk about batch norm! Any questions?"))
    assert [c["text"] for c in cues] == ["Hello everyone.", "Today we talk about batch norm!", "Any questions?"]
    check_invariants(cues)


def test_long_sentence_split_at_comma():
    text = ("So when we train a very deep network with many layers, the distribution of activations "
            "keeps shifting during training which makes optimization hard.")
    cues = split_cues(make_words(text, step=0.2))
    check_invariants(cues)
    assert len(cues) >= 2
    assert all(len(c["text"]) <= 84 for c in cues)
    assert cues[0]["text"].endswith("layers,")
    assert " ".join(c["text"] for c in cues) == text


def test_duration_limit_split_at_pause():
    # 很慢的语速，没有标点：必须按 7 秒上限切，且优先在较长停顿处
    words = make_words("one two three four five six seven eight nine ten eleven twelve", step=0.8,
                       gaps={6: 0.6})
    cues = split_cues(words)
    check_invariants(cues)
    assert all(c["end"] - c["start"] <= 7.0 + 1e-6 for c in cues)
    assert cues[0]["text"] == "one two three four five six"


def test_char_limit_without_comma_or_pause():
    words = make_words(" ".join(["word"] * 60), step=0.05)
    cues = split_cues(words, max_chars=30)
    check_invariants(cues)
    assert all(len(c["text"]) <= 30 for c in cues)
    assert sum(len(c["text"].split()) for c in cues) == 60


def test_long_silence_forces_break():
    words = make_words("and then we go on", gaps={3: 3.0})
    cues = split_cues(words)
    assert [c["text"] for c in cues] == ["and then we", "go on"]


def test_short_cue_extended_but_not_overlapping():
    words = [{"start": 0.0, "end": 0.3, "word": " Okay."},
             {"start": 0.5, "end": 2.0, "word": " Next"}, {"start": 2.0, "end": 2.5, "word": " slide."},
             {"start": 10.0, "end": 10.2, "word": " Right."}]
    cues = split_cues(words)
    check_invariants(cues)
    assert cues[0]["end"] == 0.5          # 想延长到 1 秒，但不能盖过下一条
    assert cues[2]["end"] == 11.0         # 最后一条可延长到 1 秒


def test_overlapping_word_times_fixed():
    words = [{"start": 0.0, "end": 1.2, "word": " First."}, {"start": 1.0, "end": 2.0, "word": " Second."}]
    cues = split_cues(words)
    check_invariants(cues)


def test_join_words_and_transcript_fallback():
    assert join_words([" Hello", ",", " world", "."]) == "Hello, world."
    assert join_words(["Hello", "world"]) == "Hello world"
    assert join_words(["你好", "世界", "。"]) == "你好世界。"
    tr = {"segments": [{"start": 0, "end": 2, "text": " no words here.", "words": []},
                       {"start": 2, "end": 3, "text": "Hi.", "words": [{"start": 2, "end": 3, "word": " Hi."}]}]}
    cues = split_cues(words_from_transcript(tr))
    assert [c["text"] for c in cues] == ["no words here.", "Hi."]


def test_empty():
    assert split_cues([]) == []
