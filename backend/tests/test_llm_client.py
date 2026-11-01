import dataclasses
from types import SimpleNamespace

import pytest

from app import config, db
from app.llm import client


def test_no_api_key(data_dir):
    with pytest.raises(client.LLMError, match="ANTHROPIC_API_KEY"):
        client.complete(messages=[{"role": "user", "content": "hi"}], video_id=None, stage="t")


def test_build_system_cache():
    assert client._build_system("sys", False) == "sys"
    blocks = client._build_system([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], True)
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert client._build_system("sys", True) == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]


def test_estimate_cost():
    # sonnet-5：$2 / $10 / 缓存读 $0.2 每百万
    assert client.estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000, 1_000_000) == pytest.approx(12.2)
    assert client.estimate_cost("unknown-model", 1000, 1000) == 0


@pytest.mark.parametrize("text", [
    '{"a": 1}', '```json\n{"a": 1}\n```', '好的，结果如下：\n{"a": 1}\n以上。',
])
def test_parse_json(text):
    assert client.parse_json(text) == {"a": 1}


def test_parse_json_list_and_error():
    assert client.parse_json("结果：[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        client.parse_json("没有 JSON")


class FakeStream:
    def __init__(self, msg, chunks):
        self.msg, self.text_stream = msg, iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _fake_client(chunks, captured):
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=1000,
                            cache_creation_input_tokens=0)
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text="".join(chunks))],
                          usage=usage, stop_reason="end_turn")

    def stream(**kw):
        captured.update(kw)
        return FakeStream(msg, chunks)

    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def test_complete_records_usage(data_dir, monkeypatch, set_settings):
    set_settings(dataclasses.replace(config.settings, anthropic_api_key="test-key"))
    captured = {}
    monkeypatch.setattr(client, "_get_client", lambda: _fake_client(['{"ok": ', "true}"], captured))
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
    out = client.complete_json(messages=[{"role": "user", "content": [img, img, {"type": "text", "text": "?"}]}],
                               system="s", video_id="v1", stage="read_slides", cache_system=True)
    assert out == {"ok": True}
    assert captured["model"] == config.settings.llm_model
    assert captured["system"][-1]["cache_control"] == {"type": "ephemeral"}
    row = db.fetch_one("SELECT * FROM api_usage")
    assert row["video_id"] == "v1" and row["stage"] == "read_slides" and row["images"] == 2
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50 and row["cache_read_tokens"] == 1000
    assert row["cost_usd"] == pytest.approx(client.estimate_cost(config.settings.llm_model, 100, 50, 1000))


def test_stream_yields_and_records(data_dir, monkeypatch):
    monkeypatch.setattr(client, "_get_client", lambda: _fake_client(["你", "好"], {}))
    assert list(client.stream(messages=[{"role": "user", "content": "hi"}], video_id="v", stage="qa")) == ["你", "好"]
    assert db.fetch_one("SELECT COUNT(*) AS n FROM api_usage")["n"] == 1


def test_is_reasoning_model_by_version():
    from app.llm import openai_compat as oc
    for m in ("gpt-5", "gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna", "gpt-10-x", "o1", "o3-mini", "o4-mini"):
        assert oc.is_reasoning_model(m), m
    for m in ("gpt-4.1", "gpt-4o", "deepseek-chat", "claude-x"):
        assert not oc.is_reasoning_model(m), m
    req = oc.build_request(model="gpt-6-sol", messages=[{"role": "user", "content": "hi"}], system=None,
                           max_tokens=100, effort="low", json_mode=False)
    assert req["max_completion_tokens"] == 100 and "max_tokens" not in req and req["reasoning_effort"] == "low"
