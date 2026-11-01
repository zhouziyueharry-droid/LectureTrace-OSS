"""轻量模式边界：关闭识别不导入模型、不写产物，基础服务可用。"""
from dataclasses import replace
import builtins

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.config import AsrParams
from app.pipeline import asr, runner
from app.pipeline.base import StageContext, StageError


def test_disabled_asr_preserves_artifacts(tmp_settings, set_settings, monkeypatch):
    settings = set_settings(replace(tmp_settings, asr=AsrParams(enabled=False)))
    ctx = StageContext(video_id="abc123def456", stage="asr", settings=settings)
    ctx.dir.mkdir(parents=True)
    previous = ctx.dir / "transcript.json"
    previous.write_text("existing real transcript", encoding="utf-8")
    partial = ctx.dir / asr.PARTIAL
    partial.write_text("existing checkpoint", encoding="utf-8")
    original_import = builtins.__import__

    def forbid_model(name, *args, **kwargs):
        assert not name.startswith(("faster_whisper", "ctranslate2", "nvidia"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_model)
    for call in (lambda: asr.run(ctx), lambda: asr.get_model(settings.asr)):
        with pytest.raises(StageError, match="ASR_ENABLED=true"):
            call()
    assert previous.read_text(encoding="utf-8") == "existing real transcript"
    assert partial.read_text(encoding="utf-8") == "existing checkpoint"
    assert len(list(ctx.dir.iterdir())) == 2


def test_enabled_asr_missing_dependency_has_install_hint(monkeypatch):
    import importlib.util

    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "faster_whisper" else original(name))
    with pytest.raises(StageError, match="uv sync --extra asr"):
        asr.get_model(AsrParams(enabled=True))


def test_enabled_asr_keeps_gpu_to_cpu_fallback(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(asr, "add_cuda_dll_dirs", lambda: [])
    model, calls, messages = object(), [], []

    def load(name, device, precision):
        calls.append((name, device, precision))
        if device == "cuda":
            raise RuntimeError("CUDA unavailable")
        return model

    monkeypatch.setattr(asr, "_load", load)
    assert asr.get_model(AsrParams(enabled=True), messages.append) == (model, "cpu")
    assert calls == [("large-v3", "cuda", "float16"), ("large-v3", "cpu", "int8")]
    assert any("已改用 CPU" in message for message in messages)


def test_lightweight_service_and_failed_stage(tmp_settings, set_settings):
    set_settings(replace(tmp_settings, asr=AsrParams(enabled=False)))
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/videos").status_code == 200
        assert client.post("/api/courses", json={"title": "Lightweight"}).status_code == 200
        assert client.get("/api/settings").json()["capabilities"]["asr"]["enabled"] is False
        vid = "abc123def456"
        db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)",
                   (vid, "Test", "test.mp4", db.now_iso(), "processing"))
        runner.init_stages(vid)
        assert runner._run_stage(vid, "asr") is False
        detail = client.get(f"/api/videos/{vid}").json()
        stage = next(s for s in detail["stages"] if s["name"] == "asr")
        assert stage["status"] == "failed"
        assert "ASR_ENABLED=true" in stage["error"]
        assert not (artifacts.video_dir(vid) / "transcript.json").exists()
        note = client.post(f"/api/videos/{vid}/notes", json={"name": "development.md"})
        assert note.status_code == 200
        saved = client.put(f"/api/videos/{vid}/notes/development.md",
                           json={"base_revision": note.json()["revision"], "text": "Real note"})
        assert saved.status_code == 200
        assert client.get(f"/api/videos/{vid}/notes/development.md").json()["text"] == "Real note"
