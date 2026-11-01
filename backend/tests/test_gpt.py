"""接入 GPT：token 计数与计价、OpenAI 兼容接口的请求转换与路由、设置接口、运行大纲生成、发送清单估算。
全部用假客户端，不发真实请求。"""
from __future__ import annotations

import base64
import dataclasses
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import artifacts, config, db, keys
from app.llm import client as llm, openai_compat, settings as llm_settings, tokens
from app.pipeline import runner, send_plan

from test_api import stage_map, upload, wait_status


def _jpeg_b64(w, h) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------- token 与计价 ----------

def test_image_tokens_follow_official_formula():
    assert tokens.image_tokens(2048, 2048, "gpt-5.6-sol") == 3000   # 官方文档示例
    assert tokens.image_tokens(948, 541, "gpt-5.6-sol") == 612      # 30×17 块 × 1.2
    assert tokens.image_tokens(64, 64, "other-model") == 4          # 未知模型倍率 1


def test_cost_uses_long_context_price(tmp_settings):
    assert tokens.cost_usd("gpt-5.6-sol", 200_000, 0) == pytest.approx(0.8)
    assert tokens.cost_usd("gpt-5.6-sol", 100_000, 10_000, cached_tokens=50_000) == pytest.approx(
        (50_000 * 4 + 50_000 * 0.4 + 10_000 * 20) / 1e6)
    assert tokens.cost_usd("gpt-5.6-sol", 300_000, 0) == pytest.approx(300_000 * 8 / 1e6)  # 超过 27.2 万
    assert tokens.cost_usd("unknown-model", 1000, 1000) is None


def test_request_tokens_count_text_and_images(tmp_settings):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello world"},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                      "data": _jpeg_b64(948, 541)}}]}]
    text, img = tokens.request_input_tokens(msgs, "system prompt", "gpt-5.6-sol")
    assert img == 612 and 4 < text < 40


# ---------- OpenAI 兼容接口 ----------

def test_convert_messages_and_request_params():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "看图"},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                      "data": "QUJD"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "好"}]}]
    out = openai_compat.convert_messages(msgs, "系统提示")
    assert out[0] == {"role": "system", "content": "系统提示"}
    assert out[1]["content"][1] == {"type": "image_url",
                                    "image_url": {"url": "data:image/jpeg;base64,QUJD", "detail": "high"}}
    assert out[2] == {"role": "assistant", "content": "好"}

    req = openai_compat.build_request(model="gpt-5.6-sol", messages=msgs, system=None, max_tokens=100,
                                      effort="low", json_mode=True)
    assert req["max_completion_tokens"] == 100 and req["reasoning_effort"] == "low"
    assert req["response_format"] == {"type": "json_object"} and "max_tokens" not in req
    req = openai_compat.build_request(model="deepseek-chat", messages=msgs, system=None, max_tokens=100,
                                      effort="low", json_mode=False)
    assert req["max_tokens"] == 100 and "reasoning_effort" not in req


@pytest.fixture
def openai_mode(tmp_settings, set_settings, monkeypatch):
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    keys.update({"openai": "sk-test"})
    calls = []

    class FakeCompletions:
        def create(self, **req):
            calls.append(req)
            usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=400),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=150))
            msg = SimpleNamespace(content='{"slides": []}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(openai_compat, "get_client", lambda: fake)
    return calls


def test_client_routes_to_openai_and_records_usage(openai_mode):
    llm_settings.update({"models": {"outline": "gpt-5.6-terra"}})
    out = llm.complete_json(messages=[{"role": "user", "content": "hi"}], system="s", max_tokens=500,
                            video_id="v1", stage="outline", effort="medium")
    assert out == {"slides": []}
    req = openai_mode[0]
    assert req["model"] == "gpt-5.6-terra" and req["reasoning_effort"] == "medium"
    row = db.fetch_one("SELECT * FROM api_usage")
    assert row["provider"] == "OpenAI 兼容" and row["model"] == "gpt-5.6-terra"
    assert row["input_tokens"] == 1000 and row["cache_read_tokens"] == 400
    assert row["cost_usd"] == pytest.approx((600 * 2 + 400 * 0.2 + 200 * 12) / 1e6)


def test_missing_openai_key_is_clear_error(tmp_settings, set_settings):
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    with pytest.raises(llm.LLMError, match="OpenAI API Key"):
        llm.complete(messages=[{"role": "user", "content": "hi"}], video_id=None, stage="qa")


# ---------- 设置接口 ----------

@pytest.fixture
def client(tmp_settings, fake_stages):
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_llm_settings_api(client):
    llm = client.get("/api/settings").json()["llm"]
    assert llm["models"] == {"read_slides": "gpt-5.6-sol", "outline": "gpt-5.6-sol", "qa": "gpt-5.6-sol"}
    assert llm["prices"]["gpt-5.6-sol"]["input"] == 4.0 and llm["usd_to_sgd"] == pytest.approx(1.2778)

    r = client.put("/api/settings/llm", json={"models": {"read_slides": "gpt-5.6-luna"}, "usd_to_sgd": 1.3,
                                              "prices": {"my-model": {"input": 1, "output": 2}}})
    llm = r.json()["llm"]
    assert llm["models"]["read_slides"] == "gpt-5.6-luna" and llm["models"]["outline"] == "gpt-5.6-sol"
    assert llm["usd_to_sgd"] == 1.3 and llm["prices"]["my-model"] == {"input": 1.0, "output": 2.0}
    assert llm["custom_prices"] == ["my-model"]

    llm = client.put("/api/settings/llm", json={"prices": {"my-model": None}}).json()["llm"]
    assert "my-model" not in llm["prices"]
    assert client.put("/api/settings/llm", json={"prices": {"x": {"input": 1}}}).status_code == 400
    assert client.put("/api/settings/llm", json={"usd_to_sgd": 0}).status_code == 400


def test_models_endpoint_without_key(client):
    assert client.get("/api/settings/models").json() == {"models": [], "error": "尚未填写 OpenAI API Key"}


# ---------- 运行大纲生成 ----------

def test_run_outline_for_one_video(client, fake_stages):
    db.set_setting("llm_settings", {"outline_enabled": False})
    db.set_setting("skip_confirm", True)
    vid = upload(client)
    v = wait_status(client, vid, "done")                 # 未开启：7～9 步跳过，DeepL 之外无需确认
    assert stage_map(v)["outline"]["error"] == runner.DISABLED_REASON

    v = client.post(f"/api/videos/{vid}/outline/run").json()
    v = wait_status(client, vid, "awaiting_confirm")    # 要调用 GPT：即使"以后不再询问"也先停下确认
    assert stage_map(v)["translate"]["status"] == "done"
    client.post(f"/api/videos/{vid}/confirm", json={})
    v = wait_status(client, vid, "done")
    assert all(stage_map(v)[n]["status"] == "done" for n in runner.OUTLINE_STAGES)
    assert fake_stages.calls.count("translate") == 1     # 翻译不会重跑

    client.delete(f"/api/videos/{vid}")
    assert db.get_setting(f"outline_run:{vid}") is None


# ---------- 发送清单：GPT 阶段估算 ----------

def test_send_plan_estimates_gpt_cost_in_sgd(tmp_settings, set_settings, monkeypatch):
    set_settings(dataclasses.replace(config.settings, llm_provider="openai",
                                     translate=config.TranslateParams(provider="deepl")))
    keys.update({"deepl": "k:fx", "openai": "sk-test"})
    monkeypatch.setattr(send_plan, "deepl_usage", lambda key: None)
    vid = "vgpt"
    d = artifacts.video_dir(vid) / "keyframes"
    d.mkdir(parents=True)
    kfs = []
    for i in range(1, 8):
        Image.new("RGB", (948, 541), "white").save(d / f"S{i:03d}.jpg")
        kfs.append({"id": f"S{i:03d}", "image": f"keyframes/S{i:03d}.jpg", "appearances": [[i * 10, i * 10 + 9]]})
    artifacts.write_artifact(vid, "keyframes.json", kfs, {})
    artifacts.write_artifact(vid, "cues.json", [{"id": f"C{i:04d}", "start": i, "end": i + 1,
                                                  "text": "gradient descent", "zh": "译"} for i in range(80)], {})
    plan = send_plan.build_send_plan(vid)
    items = {i["stage"]: i for i in plan["items"]}
    assert items["translate"]["skipped"] is True                     # 已全部翻译，无需发送
    rs = items["read_slides"]
    assert rs["image_tokens"] == 7 * 612 and "分 2 次请求" in rs["desc"]
    assert rs["input_tokens_range"][0] > rs["image_tokens"]
    lo, hi = plan["cost_usd_range"]
    assert 0 < lo < hi
    assert plan["cost_sgd_range"] == [round(lo * 1.2778, 2), round(hi * 1.2778, 2)]
    assert plan["blocking"] == []

    keys.update({"openai": ""})
    assert "未填写 OpenAI API Key，请到设置页填写" in send_plan.build_send_plan(vid)["blocking"]


def test_effort_setting_changes_requests_and_estimate(openai_mode, monkeypatch):
    # 默认：读幻灯片 low、大纲 medium；改成 high 后请求带上 high，费用预估的输出区间变大
    assert llm_settings.effort_for("read_slides") == "low" and llm_settings.effort_for("outline") == "medium"
    est_low = send_plan._outline_estimate("v", [{"id": "S001"}], [{"start": 0, "end": 1, "text": "hi"}])
    llm_settings.update({"efforts": {"outline": "high"}})
    llm.complete_json(messages=[{"role": "user", "content": "x"}], video_id=None, stage="outline",
                      effort=llm_settings.effort_for("outline"))
    assert openai_mode[-1]["reasoning_effort"] == "high"
    est_high = send_plan._outline_estimate("v", [{"id": "S001"}], [{"start": 0, "end": 1, "text": "hi"}])
    assert est_high["output_range"][1] > est_low["output_range"][1] and est_high["effort"] == "high"
    with pytest.raises(ValueError, match="未知的思考强度"):
        llm_settings.update({"efforts": {"qa": "ultra"}})


def test_effort_api_validation(client):
    r = client.put("/api/settings/llm", json={"efforts": {"read_slides": "none", "qa": "xhigh"}})
    llm = r.json()["llm"]
    assert llm["efforts"] == {"read_slides": "none", "outline": "medium", "qa": "xhigh"}
    assert llm["effort_options"] == ["none", "low", "medium", "high", "xhigh", "max"]
    assert client.put("/api/settings/llm", json={"efforts": {"qa": "ultra"}}).status_code == 400


# ---------- 提示缓存 ----------

def test_one_off_requests_do_not_write_cache(tmp_settings):
    # 官方接口：一律显式模式；没有 cache_key 时不设缓存点，GPT-5.6 就不会自动写缓存（写入按 1.25× 收费）
    req = openai_compat.build_request(model="gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}],
                                      system="资料", max_tokens=10, effort="low", json_mode=False)
    assert req["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}
    assert req["messages"][0] == {"role": "system", "content": "资料"}


def test_qa_request_sets_cache_breakpoint_and_key(tmp_settings):
    req = openai_compat.build_request(model="gpt-5.6-sol", messages=[{"role": "user", "content": "问题"}],
                                      system=[{"type": "text", "text": "说明"}, {"type": "text", "text": "证据"}],
                                      max_tokens=10, effort="low", json_mode=False, cache_key="k1")
    assert req["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}, "prompt_cache_key": "k1"}
    assert req["messages"][0]["content"] == [
        {"type": "text", "text": "说明\n\n证据", "prompt_cache_breakpoint": {"mode": "explicit"}}]


def test_other_providers_get_no_cache_params(tmp_settings):
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    req = openai_compat.build_request(model="deepseek-chat", messages=[{"role": "user", "content": "hi"}],
                                      system="资料", max_tokens=10, effort=None, json_mode=False, cache_key="k1")
    assert "extra_body" not in req and req["messages"][0] == {"role": "system", "content": "资料"}


def test_cost_counts_cache_writes_and_hits(tmp_settings):
    # 1000 输入：200 命中（0.40）、300 写入（4.00×1.25）、500 普通（4.00）；100 输出（20.00）
    expected = (500 * 4 + 200 * 0.4 + 300 * 5 + 100 * 20) / 1e6
    assert tokens.cost_usd("gpt-5.6-sol", 1000, 100, cached_tokens=200, cache_write_tokens=300) == pytest.approx(expected)


def test_usage_reads_cache_write_tokens():
    u = SimpleNamespace(prompt_tokens=1567, completion_tokens=20,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=1555),
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
    usage = openai_compat._usage(u)
    assert usage.cache_write_tokens == 1555 and usage.cached_tokens == 0


def test_qa_stream_passes_cache_key(openai_mode, monkeypatch):
    seen = []

    def fake_stream(req, on_usage):
        seen.append(req)
        on_usage(openai_compat.Usage(input_tokens=60000, output_tokens=100, cached_tokens=59000))
        yield "答"

    monkeypatch.setattr(openai_compat, "stream", fake_stream)
    out = "".join(llm.stream(messages=[{"role": "user", "content": "q"}], system=[{"type": "text", "text": "证据"}],
                             video_id="v9", stage="qa", cache_system=True))
    assert out == "答"
    assert seen[0]["extra_body"]["prompt_cache_key"] == "lecturetrace-qa-v9"
    row = db.fetch_one("SELECT * FROM api_usage")
    assert row["cache_read_tokens"] == 59000
