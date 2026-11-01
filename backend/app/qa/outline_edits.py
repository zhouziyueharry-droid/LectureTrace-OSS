"""Revision-checked outline previews; one atomic artifact contains data and undo history."""
from __future__ import annotations

from copy import deepcopy
import difflib
import hashlib
import json
import math
import re
import threading
import uuid

from .. import artifacts, db
from ..config import SCHEMA_VERSION
from ..pipeline.evidence import id_index

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class OutlineEditError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def lock(video_id: str):
    # ponytail: one backend process; use database locks if multiple workers are introduced.
    key = str(artifacts.video_dir(video_id).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def evidence_version(video_id: str) -> str:
    return fingerprint(artifacts.read_artifact(video_id, "evidence.json"))


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _nodes(outline):
    for chapter in outline.get("chapters", []):
        yield "chapter", chapter
        for child in chapter.get("children", []):
            yield "child", child
    for item in outline.get("admin", {}).get("items", []):
        yield "admin", item


def _revision():
    return uuid.uuid4().hex


def _identity(kind, node):
    evidence = node.get("evidence", []) or [
        i for child in node.get("children", []) for i in child.get("evidence", [])]
    return kind, tuple(sorted(set(evidence)))


def _read(video_id):
    return artifacts.read_json(artifacts.video_dir(video_id) / "outline.json")


def ensure(video_id: str) -> dict:
    """Migrate legacy outlines once, without changing stage params or evidence."""
    with lock(video_id):
        obj = _read(video_id)
        outline = obj["data"]
        changed = not outline.get("revision")
        ids = set()
        for _, node in _nodes(outline):
            if not node.get("node_id"):
                node["node_id"] = "O" + _revision()
                changed = True
            if not isinstance(node["node_id"], str) or node["node_id"] in ids:
                raise OutlineEditError("大纲节点 ID 无效或重复")
            ids.add(node["node_id"])
        if changed:
            outline["revision"] = _revision()
            try:
                obj["outline_evidence_version"] = evidence_version(video_id)
            except FileNotFoundError:
                obj["outline_evidence_version"] = None
            obj.setdefault("outline_history", [])
            artifacts.write_json(artifacts.video_dir(video_id) / "outline.json", obj)
        return deepcopy(outline)


def draft(video_id: str) -> dict:
    outline = ensure(video_id)
    return {"base_revision": outline["revision"], "outline": outline}


def _check_revision(base, current):
    if not isinstance(base, str) or base != current["revision"]:
        raise OutlineEditError("大纲版本已变化，请刷新后重新生成修改预览", 409)


def _validate(candidate, current, evidence):
    """Editing is strict; pipeline validate() deliberately salvages imperfect model output."""
    if not isinstance(candidate, dict) or set(candidate) - {
        "chapters", "admin", "speech_categories", "warnings", "revision"
    }:
        raise OutlineEditError("大纲结构无效")
    after = deepcopy(candidate)
    for key in ("speech_categories", "warnings"):
        if key in after and after[key] != current.get(key):
            raise OutlineEditError(f"不能通过大纲编辑修改 {key}")
        if key in current:
            after[key] = deepcopy(current[key])
    if "revision" in after and after["revision"] != current["revision"]:
        raise OutlineEditError("不能修改大纲 revision")
    after["revision"] = current["revision"]
    admin = after.get("admin")
    if (not isinstance(after.get("chapters"), list) or not isinstance(admin, dict)
            or set(admin) != {"title", "items"} or not isinstance(admin["title"], str)
            or not isinstance(admin["items"], list)):
        raise OutlineEditError("大纲章节或课程事务结构无效")
    index = id_index(evidence)
    known_ids = {node["node_id"] for _, node in _nodes(current)}
    used = set()

    def node_check(node, depth, is_admin=False):
        if not isinstance(node, dict) or set(node) - {
            "node_id", "title", "summary", "evidence", "start", "children"
        }:
            raise OutlineEditError("大纲节点结构无效")
        if not isinstance(node.get("title"), str) or not node["title"].strip():
            raise OutlineEditError("节点标题必须是非空文本")
        if not isinstance(node.get("summary"), str):
            raise OutlineEditError("节点摘要必须是文本")
        ev = node.get("evidence")
        if (not isinstance(ev, list) or any(not isinstance(i, str) or i not in index for i in ev)
                or len(set(ev)) != len(ev)):
            raise OutlineEditError("节点证据 ID 无效或重复")
        if "start" in node and (type(node["start"]) not in (int, float)
                                or not math.isfinite(node["start"])):
            raise OutlineEditError("节点时间无效")
        children = node.get("children", [])
        if not isinstance(children, list) or ((depth or is_admin) and children):
            raise OutlineEditError("大纲最多两层，课程事务不能包含子节点")
        nid = node.get("node_id")
        if nid is None:
            nid = "O" + _revision()
            node["node_id"] = nid
        elif not isinstance(nid, str) or nid not in known_ids:
            raise OutlineEditError("节点 ID 未知；新节点请省略 node_id")
        if nid in used:
            raise OutlineEditError("大纲节点 ID 重复")
        used.add(nid)
        times = [index[i]["jump"] for i in ev]
        times.extend(node_check(child, 1) for child in children)
        if not times:
            raise OutlineEditError("节点必须包含有效证据或有证据的子节点")
        node["start"] = min(times)
        if is_admin:
            node.pop("children", None)
        else:
            node["children"] = children
        return node["start"]

    for chapter in after["chapters"]:
        node_check(chapter, 0)
    for item in admin["items"]:
        node_check(item, 1, True)
    return after


def prepare(video_id: str, draft: dict) -> dict:
    with lock(video_id):
        before = ensure(video_id)
        if not isinstance(draft, dict) or set(draft) != {"base_revision", "outline"}:
            raise OutlineEditError("修改草稿需要 base_revision 与 outline")
        _check_revision(draft["base_revision"], before)
        evidence = artifacts.read_artifact(video_id, "evidence.json")
        after = _validate(draft["outline"], before, evidence)
        def lines(data):
            return json.dumps(data, ensure_ascii=False, indent=2).splitlines(keepends=True)
        preview = {"proposal_id": _revision(), "base_revision": before["revision"],
                   "before": before, "after": after,
                   "diff": "".join(difflib.unified_diff(lines(before), lines(after),
                                                       fromfile="修改前", tofile="修改后"))}
        artifacts.write_json(_proposal_path(video_id, preview["proposal_id"]),
                             {**preview, "evidence_version": fingerprint(evidence)})
        return preview


def _proposal_path(video_id, proposal_id):
    if not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise OutlineEditError("修改预览 ID 无效")
    return artifacts.video_dir(video_id) / "outline_edits" / (proposal_id + ".json")


def _save(video_id, obj, after, version, *, history=None, params=None):
    if history is None:
        history = obj.get("outline_history", []) + [{
            "outline": deepcopy(obj["data"]), "evidence_version": obj.get("outline_evidence_version")}]
    after = deepcopy(after)
    after["revision"] = _revision()
    obj = {**obj, "data": after, "created_at": db.now_iso(), "outline_history": history,
           "outline_evidence_version": version}
    if params is not None:
        obj["params"] = params
    artifacts.write_json(artifacts.video_dir(video_id) / "outline.json", obj)
    return after


def commit(video_id: str, proposal_id: str) -> dict:
    with lock(video_id):
        current = ensure(video_id)
        try:
            preview = artifacts.read_json(_proposal_path(video_id, proposal_id))
        except FileNotFoundError:
            raise OutlineEditError("修改预览不存在，请重新生成") from None
        _check_revision(preview["base_revision"], current)
        version = evidence_version(video_id)
        if preview["evidence_version"] != version:
            raise OutlineEditError("课程证据已变化，请重新生成修改预览", 409)
        return _save(video_id, _read(video_id), preview["after"], version)


def undo(video_id: str, base_revision: str) -> dict:
    with lock(video_id):
        current = ensure(video_id)
        _check_revision(base_revision, current)
        obj = _read(video_id)
        history = obj.get("outline_history", [])
        if not history:
            raise OutlineEditError("没有可以撤销的大纲修改")
        previous = history[-1]
        version = evidence_version(video_id)
        if previous["evidence_version"] != version:
            raise OutlineEditError("课程证据已变化，无法撤销到旧版大纲", 409)
        return _save(video_id, obj, previous["outline"], version, history=history[:-1])


def persist_generated(ctx, outline):
    """Match nodes by evidence identity; generation never overwrites concurrent edits."""
    with lock(ctx.video_id):
        try:
            previous = ensure(ctx.video_id)
            obj = _read(ctx.video_id)
        except FileNotFoundError:
            previous = {"chapters": [], "admin": {"items": []}, "revision": None}
            obj = {"schema_version": SCHEMA_VERSION, "params": ctx.params, "data": previous}
        if (hasattr(ctx, "outline_base_revision")
                and ctx.outline_base_revision != previous["revision"]):
            raise OutlineEditError("生成期间大纲已被修改，请重新运行生成", 409)
        version = evidence_version(ctx.video_id)
        if hasattr(ctx, "outline_evidence_version") and ctx.outline_evidence_version != version:
            raise OutlineEditError("生成期间课程证据已变化，请重新运行生成", 409)
        identities = {}
        for kind, node in _nodes(previous):
            key = _identity(kind, node)
            identities.setdefault(key, []).append(node["node_id"])
        result = deepcopy(outline)
        for kind, node in _nodes(result):
            matches = identities.get(_identity(kind, node), [])
            node["node_id"] = matches.pop(0) if matches else "O" + _revision()
        history = None if previous["revision"] else []
        return _save(ctx.video_id, obj, result, version, params=ctx.params, history=history)
