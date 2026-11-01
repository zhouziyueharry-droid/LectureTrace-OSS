"""Outline editing is preview-only until commit, with revision and evidence guards."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import artifacts
from app.pipeline.base import StageContext
from app.qa import outline_edits as edits


@pytest.fixture
def course(tmp_settings):
    evidence = {"pages": [], "sections": [], "speech": [
        {"id": "T0001", "start": 10, "end": 20, "jump": 10, "text": "first"},
        {"id": "T0002", "start": 30, "end": 40, "jump": 30, "text": "second"}]}
    outline = {"chapters": [{"title": "A", "summary": "", "evidence": ["T0001"], "start": 10,
                             "children": []}], "admin": {"title": "课程事务", "items": []},
               "speech_categories": {}, "warnings": []}
    artifacts.write_artifact("vo", "evidence.json", evidence, {})
    artifacts.write_artifact("vo", "outline.json", outline, {"test": "preserved"})
    return "vo"


def test_migration_preview_commit_undo(course):
    before = edits.ensure(course)
    assert before == edits.ensure(course)
    assert before["revision"] and before["chapters"][0]["node_id"]
    change = edits.draft(course)
    change["outline"]["chapters"][0].update(title="Changed", evidence=["T0002"], start=999)
    preview = edits.prepare(course, change)
    assert edits.ensure(course) == before
    assert preview["after"]["chapters"][0]["start"] == 30
    assert "Changed" in preview["diff"]
    after = edits.commit(course, preview["proposal_id"])
    assert after["revision"] != before["revision"]
    assert after["chapters"][0]["node_id"] == before["chapters"][0]["node_id"]
    restored = edits.undo(course, after["revision"])
    assert restored["chapters"] == before["chapters"]
    assert restored["revision"] not in {after["revision"], before["revision"]}
    assert artifacts.read_artifact_meta(course, "outline.json")["params"] == {"test": "preserved"}
    with pytest.raises(edits.OutlineEditError) as error:
        edits.commit(course, preview["proposal_id"])
    assert error.value.status_code == 409


@pytest.mark.parametrize("mutation", [
    lambda n: n.update(evidence=["BAD"]),
    lambda n: n.update(evidence=["T0001", "T0001"]),
    lambda n: n.update(evidence="T0001"),
    lambda n: n.update(title=42),
    lambda n: n.update(children=[{"title": "x", "summary": "", "evidence": ["T0001"],
                                  "children": [{"title": "too deep"}]}]),
    lambda n: n.update(node_id="forged"),
    lambda n: n.update(surprise=True),
    lambda n: n.update(evidence=[]),
])
def test_strict_draft_validation(course, mutation):
    change = edits.draft(course)
    mutation(change["outline"]["chapters"][0])
    with pytest.raises(edits.OutlineEditError) as error:
        edits.prepare(course, change)
    assert error.value.status_code == 400


def test_duplicate_ids_and_new_node(course):
    change = edits.draft(course)
    change["outline"]["chapters"].append(deepcopy(change["outline"]["chapters"][0]))
    with pytest.raises(edits.OutlineEditError):
        edits.prepare(course, change)
    change["outline"]["chapters"][1].pop("node_id")
    preview = edits.prepare(course, change)
    ids = [n["node_id"] for n in preview["after"]["chapters"]]
    assert len(set(ids)) == 2


def test_revision_evidence_and_path_conflicts(course):
    change = edits.draft(course)
    change["base_revision"] = "stale"
    with pytest.raises(edits.OutlineEditError) as error:
        edits.prepare(course, change)
    assert error.value.status_code == 409
    preview = edits.prepare(course, edits.draft(course))
    ev = artifacts.read_artifact(course, "evidence.json")
    ev["speech"][0]["text"] = "regenerated"
    artifacts.write_artifact(course, "evidence.json", ev, {})
    with pytest.raises(edits.OutlineEditError) as error:
        edits.commit(course, preview["proposal_id"])
    assert error.value.status_code == 409
    with pytest.raises(edits.OutlineEditError):
        edits.commit(course, "../../outline")


def test_generation_preserves_identity_and_invalidates_preview(course):
    old = edits.ensure(course)
    preview = edits.prepare(course, edits.draft(course))
    generated = deepcopy(old)
    generated.pop("revision")
    generated["chapters"][0].pop("node_id")
    generated["chapters"][0]["title"] = "New title"
    edits.persist_generated(StageContext(video_id=course, stage="outline"), generated)
    result = edits.ensure(course)
    assert result["chapters"][0]["node_id"] == old["chapters"][0]["node_id"]
    assert result["revision"] != old["revision"]
    with pytest.raises(edits.OutlineEditError) as error:
        edits.commit(course, preview["proposal_id"])
    assert error.value.status_code == 409


def test_concurrent_commit_only_one_wins(course):
    previews = [edits.prepare(course, edits.draft(course)) for _ in range(2)]
    def commit(preview):
        try:
            return edits.commit(course, preview["proposal_id"])["revision"]
        except edits.OutlineEditError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, previews))
    assert results.count(409) == 1


def test_failed_atomic_write_does_not_change_outline_or_history(course, monkeypatch):
    before = edits.ensure(course)
    preview = edits.prepare(course, edits.draft(course))
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(artifacts, "write_json", fail)
    with pytest.raises(OSError):
        edits.commit(course, preview["proposal_id"])
    assert edits.ensure(course) == before


def test_generation_detects_edits_made_while_model_runs(course):
    original = edits.ensure(course)
    ctx = StageContext(video_id=course, stage="outline")
    ctx.outline_base_revision = original["revision"]
    ctx.outline_evidence_version = edits.evidence_version(course)
    change = edits.draft(course)
    change["outline"]["chapters"][0]["title"] = "User edit"
    committed = edits.commit(course, edits.prepare(course, change)["proposal_id"])
    with pytest.raises(edits.OutlineEditError) as error:
        edits.persist_generated(ctx, original)
    assert error.value.status_code == 409
    assert edits.ensure(course) == committed


def test_generated_parent_identity_uses_children_when_own_evidence_is_empty(course):
    change = edits.draft(course)
    child = change["outline"]["chapters"][0]
    change["outline"]["chapters"] = [{"title": "Parent", "summary": "", "evidence": [],
                                        "children": [child]}]
    before = edits.commit(course, edits.prepare(course, change)["proposal_id"])
    generated = deepcopy(before)
    inserted = {"title": "Different parent", "summary": "", "evidence": [], "children": [
        {"title": "Different child", "summary": "", "evidence": ["T0002"], "children": []}]}
    generated["chapters"].insert(0, inserted)
    edits.persist_generated(StageContext(video_id=course, stage="outline"), generated)
    after = edits.ensure(course)
    assert after["chapters"][1]["node_id"] == before["chapters"][0]["node_id"]
    assert after["chapters"][0]["node_id"] != before["chapters"][0]["node_id"]


def test_http_commit_and_undo_return_revision_and_conflict(course, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import outline_edits as api
    monkeypatch.setattr(api, "get_video", lambda video_id: {})
    app = FastAPI()
    app.include_router(api.router)
    before = edits.ensure(course)
    change = edits.draft(course)
    change["outline"]["chapters"][0]["summary"] = "Updated"
    preview = edits.prepare(course, change)
    with TestClient(app) as client:
        url = f"/api/videos/{course}/outline"
        response = client.post(url + "/commit", json={"proposal_id": preview["proposal_id"]})
        assert response.status_code == 200
        current = response.json()
        assert current["revision"] != before["revision"]
        assert client.post(url + "/commit", json={"proposal_id": preview["proposal_id"]}).status_code == 409
        assert client.post(url + "/undo", json={"base_revision": before["revision"]}).status_code == 409
        response = client.post(url + "/undo", json={"base_revision": current["revision"]})
        assert response.status_code == 200 and response.json()["chapters"] == before["chapters"]
        assert client.post(url + "/commit", json={"proposal_id": "../outline"}).status_code == 400
