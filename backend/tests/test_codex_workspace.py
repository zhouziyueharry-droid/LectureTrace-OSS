"""无模型验收：导航、取证、读取日志和虚拟工作区权限。"""
import copy
import json

import pytest
from PIL import Image

from app import artifacts
from app.qa import notes, workspace


VID = "abc123def456"


@pytest.fixture
def course(tmp_settings, tmp_path, monkeypatch):
    evidence = {
        "pages": [{"id": "S001", "title": "Normalization", "visual_content": "Batch normalization gamma formula.",
                   "jump": 10, "sections": ["S001.1"], "image": "keyframes/S001.jpg"},
                  {"id": "S002", "title": "Layer Norm", "visual_content": "Layer normalization formula.",
                   "jump": 100, "sections": ["S002.1"], "image": "keyframes/S002.jpg"}],
        "sections": [{"id": "N001", "page": None, "jump": 0, "speech_end": 10,
                      "t_ids": ["T0001"], "spoken_summary": ""},
                     {"id": "S001.1", "page": "S001", "jump": 10, "display_end": 90,
                      "t_ids": ["T0002", "T0003"], "spoken_summary": "Why normalize each mini batch."},
                     {"id": "S002.1", "page": "S002", "jump": 100, "display_end": 150,
                      "t_ids": ["T0004"], "spoken_summary": "Layer norm works per example."}],
        "speech": [{"id": "T0001", "section": "N001", "jump": 0, "text": "Unmapped opening topic."},
                   {"id": "T0002", "section": "S001.1", "jump": 10, "text": "Batch normalization normalizes each mini batch."},
                   {"id": "T0003", "section": "S001.1", "jump": 50, "text": "Gamma restores normalization capacity."},
                   {"id": "T0004", "section": "S002.1", "jump": 100, "text": "Layer normalization works per example."}],
    }
    outline = {"revision": "r1", "chapters": [
        {"node_id": "n1", "title": "批归一化", "summary": "BN 与缩放", "start": 10,
         "evidence": ["S001.1"], "children": []},
        {"node_id": "n2", "title": "层归一化", "summary": "LN", "start": 100,
         "evidence": ["S002.1"], "children": []},
    ], "admin": {"title": "课程事务", "items": []}}
    path = artifacts.video_dir(VID) / "evidence.json"
    artifacts.write_json(path, {"created_at": "v1", "data": evidence})
    image = path.parent / "keyframes/S001.jpg"
    image.parent.mkdir()
    Image.new("RGB", (32, 32), "white").save(image)
    monkeypatch.setattr(workspace.outline_edits, "ensure", lambda _: copy.deepcopy(outline))
    monkeypatch.setattr(workspace.outline_edits, "draft", lambda _: {"base_revision": "r1", "outline": copy.deepcopy(outline)})
    options = {"workspace_root": str(tmp_path / "workspaces"), "max_tool_calls": 30,
               "max_read_tokens": 12000, "max_images": 4, "search_k": 8, "snippet_chars": 200}
    return evidence, outline, options


def build(course, conversation="c1"):
    ws = workspace.CourseWorkspace(VID, conversation, course[2])
    ws.begin_turn("turn1")
    return ws


def text(result):
    return result["contentItems"][0]["text"]


def test_navigation_has_full_timeline_but_no_evidence_body(course):
    ws = build(course)
    assert "N001" in ws.entry and "T0001" in ws.entry
    assert "n1" in ws.entry and "批归一化" in ws.entry
    assert "Unmapped opening topic" not in ws.entry
    assert "Gamma restores" not in ws.entry
    assert "Batch normalization gamma formula" not in ws.entry
    assert ws.navigation_ids == {"N001", "S001.1", "S002.1"}
    nav = ws.call("read_nav", {"path": "chapters/01.md"})
    assert nav["success"] and "Why normalize" in text(nav)
    assert not ws.read_ids


def test_multilingual_search_is_diverse_and_not_read_proof(course):
    ws = build(course)
    response = ws.call("search", {"queries": ["归一化", "normalization"], "k": 2})
    hits = json.loads(text(response))["results"]
    assert response["success"] and {hit["chapter"] for hit in hits} == {1, 2}
    response = ws.call("search", {"queries": ["layer"], "chapter": "n2", "k": 2})
    assert all(hit["chapter"] == 2 for hit in json.loads(text(response))["results"])
    assert not ws.read_ids


def test_ranges_are_logged_by_conversation_version_and_turn(course):
    ws = build(course)
    result = ws.call("read_evidence", {"ids": ["T0002-T0003"]})
    assert result["success"] and "Gamma restores" in text(result)
    assert ws.read_ids == {"T0002", "T0003"}
    ws.begin_turn("turn2")
    assert ws.call("read_evidence", {"ids": ["S001"]})["success"]
    log = artifacts.read_json(ws.log_path)
    assert log["versions"][ws.version] == {"turn1": ["T0002", "T0003"], "turn2": ["S001"]}
    assert build(course).read_ids == {"T0002", "T0003", "S001"}
    assert not build(course, "another").read_ids


def test_legacy_limits_do_not_interrupt_read_edit_and_verify(course):
    course[2].update(max_read_tokens=1, max_tool_calls=1, max_images=0)
    ws = build(course)
    for _ in range(31):
        assert ws.call('read_evidence', {'ids': ['T0002']})['success']
    for _ in range(5):
        assert ws.call('view_slide', {'id': 'S001'})['success']
    body = ('课堂讲解与公式 [T0002]\n' * 2500)
    assert ws.call('write_note', {'path': 'notes/review.md', 'text': body})['success']
    first = json.loads(text(ws.call('read_nav', {'path': 'notes/review.md'})))
    assert set(first) == {'name', 'text', 'revision', 'proposal'}
    assert first['text'] == body
    assert ws.call('read_nav', {'path': 'notes/review.md'})['success']
    assert ws.tokens > 12000 and ws.calls > 30 and ws.images == 5
    assert ws.call('write_note', {'path': 'notes/review.md', 'text': body + '补充',
                                 'base_revision': first['revision']})['success']
    assert notes.read(VID, 'review.md')['text'] == body
    assert notes.proposal(VID, 'review.md')['text'] == body + '补充'


def test_version_change_blocks_tools_without_losing_notes(course):
    ws = build(course)
    assert ws.call("write_note", {"path": "notes/review.md", "text": "Remember the question."})["success"]
    ws.call("read_evidence", {"ids": ["T0002"]})
    course[0]["speech"][0]["text"] = "Changed evidence, same second."
    artifacts.write_json(artifacts.video_dir(VID) / "evidence.json", {"created_at": "v1", "data": course[0]})
    assert not ws.call("read_evidence", {"ids": ["T0003"]})["success"]
    rebuilt = build(course)
    assert not rebuilt.read_ids
    assert json.loads(text(rebuilt.call("read_nav", {"path": "notes/review.md"})))["text"] == "Remember the question."
    course[1]["revision"] = "r2"
    assert json.loads(text(build(course).call("read_nav", {"path": "notes/review.md"})))["text"] == "Remember the question."
    rebuilt.clear_notes()
    assert rebuilt.call("read_nav", {"path": "notes/review.md"})["success"]


def test_shared_notes_reject_stale_agent_write_and_unread_citations(course):
    a, b = build(course), build(course, 'c2')
    assert a.call('write_note', {'path': 'notes/共享.md', 'text': '初稿', 'base_revision': None})['success']
    old = json.loads(text(b.call('read_nav', {'path': 'notes/共享.md'})))
    assert old['text'] == '初稿'
    # 已有笔记只产出建议，原文不变；用户（或别的写入）改过之后，旧 revision 的建议被拒绝。
    assert a.call('write_note', {'path': 'notes/共享.md', 'text': '手动修订', 'base_revision': old['revision']})['success']
    assert notes.read(VID, '共享.md')['text'] == '初稿'
    assert notes.proposal(VID, '共享.md')['text'] == '手动修订'
    notes.save(VID, '共享.md', '用户改动', old['revision'])
    assert not b.call('write_note', {'path': 'notes/共享.md', 'text': '过期覆盖', 'base_revision': old['revision']})['success']
    assert not a.call('write_note', {'path': 'notes/虚构.md', 'text': '结论 [T0002]', 'base_revision': None})['success']
    assert a.call('read_evidence', {'ids': ['T0002']})['success']
    assert a.call('write_note', {'path': 'notes/依据.md', 'text': '结论 [T0002]', 'base_revision': None})['success']


def test_path_validation_and_unknown_tools_fail_closed(course):
    ws = build(course)
    for path in ("../evidence.json", "C:/Windows/win.ini", "/evidence.json", "notes/../../secret", "notes\\x"):
        assert not ws.call("read_nav", {"path": path})["success"]
        assert not ws.call("write_note", {"path": path, "text": "x"})["success"]
    assert not ws.call("read_nav", {"path": "evidence.json"})["success"]
    assert not ws.call("write_note", {"path": "map.md", "text": "x"})["success"]
    assert not ws.call("shell", {"command": "whoami"})["success"]
    assert not ws.call("read_evidence", {"ids": ["T9999"]})["success"]
    assert not ws.call("read_evidence", {"ids": ["T0003-T0002"]})["success"]
    assert not ws.call("search", {"queries": ["x"], "k": 0})["success"]
    capped = ws.call("search", {"queries": ["x"], "k": 999})
    assert capped["success"]
    assert len(json.loads(capped["contentItems"][0]["text"])["results"]) <= ws.options["search_k"]
    assert not ws.call("search", {"queries": ["x"], "chapter": "missing"})["success"]


def test_range_note_requires_every_member_to_have_been_read(course):
    ws = build(course)
    assert ws.call('read_evidence', {'ids': ['T0002', 'T0004']})['success']
    args = {'path': 'notes/range.md', 'text': '结论 [T0002-T0004]', 'base_revision': None}
    assert not ws.call('write_note', args)['success']
    assert ws.call('read_evidence', {'ids': ['T0003']})['success']
    assert ws.call('write_note', args)['success']
    assert [c['id'] for c in notes.read(VID, 'range.md')['citations']] == ['T0002', 'T0003', 'T0004']


def test_screenshot_cannot_escape_video_directory(course, tmp_path):
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (32, 32)).save(outside)
    course[0]["pages"][0]["image"] = str(outside)
    artifacts.write_json(artifacts.video_dir(VID) / "evidence.json", {"created_at": "v1", "data": course[0]})
    ws = build(course)
    result = ws.call("view_slide", {"id": "S001"})
    assert not result["success"] and str(outside) not in text(result)
    assert not ws.read_ids


def test_outline_tool_requires_user_authorization_and_only_prepares(course, monkeypatch):
    ws = build(course)
    called = []
    preview = {"proposal_id": "p1", "base_revision": "r1", "before": course[1], "after": course[1], "diff": ""}
    monkeypatch.setattr(workspace.outline_edits, "prepare", lambda vid, draft: called.append((vid, draft)) or preview)
    draft = {"base_revision": "r1", "outline": course[1]}
    assert not ws.call("edit_outline", {"draft": draft})["success"]
    assert not called and ws.pending_edit is None
    ws.begin_turn("edit", allow_outline_edit=True)
    assert ws.call("edit_outline", {"draft": draft})["success"]
    assert called == [(VID, draft)] and ws.pending_edit == preview
    ws.begin_turn("normal")
    assert ws.pending_edit is None


def test_empty_body_is_not_marked_as_read_and_navigation_charges_budget(course):
    ws = build(course)
    assert ws.call("read_evidence", {"ids": ["N001"]})["success"]
    assert not ws.read_ids
    assert ws.calls == 1 and ws.tokens > 0
    listing = ws.call("ls", {})
    assert listing["success"] and "map.md" in text(listing) and "chapters/" in text(listing)
    assert ws.calls == 2


def test_project_directory_cannot_be_used_as_course_workspace(course):
    course[2]["workspace_root"] = str(workspace.ROOT_DIR / "data/workspaces")
    with pytest.raises(workspace.WorkspaceError, match="仓库目录之外"):
        build(course)


def test_evidence_only_course_can_answer_without_creating_outline(course, monkeypatch):
    def missing(_):
        raise FileNotFoundError()
    monkeypatch.setattr(workspace.outline_edits, "ensure", missing)
    ws = build(course)
    assert ws.revision == "none" and "N001" in ws.entry
    assert not (artifacts.video_dir(VID) / "outline.json").exists()
    assert ws.call("read_evidence", {"ids": ["T0001"]})["success"]
    ws.begin_turn("edit", allow_outline_edit=True)
    assert not ws.call("edit_outline", {"draft": {}})["success"]


def test_missing_tokenizer_only_affects_usage_estimate(course, monkeypatch):
    monkeypatch.setattr(workspace, "_encoding", lambda: False)
    assert workspace._token_count("中文") == 6
    course[2]["max_read_tokens"] = 5
    ws = build(course)
    assert ws.call("write_note", {"path": "notes/x.md", "text": "中文"})["success"]


def test_another_git_checkout_cannot_be_a_workspace(course, tmp_path):
    repo = tmp_path / "other_repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere")
    course[2]["workspace_root"] = str(repo / "workspaces")
    with pytest.raises(workspace.WorkspaceError, match="Git"):
        build(course)


def test_tool_specs_include_app_server_function_discriminator():
    specs = workspace.CourseWorkspace.tool_specs()
    assert len(specs) == 13
    assert all(spec["type"] == "function" and spec["inputSchema"]["type"] == "object" for spec in specs)


def test_conversation_notes_cleanup_does_not_load_deleted_evidence(course, monkeypatch):
    ws = build(course)
    old = ws.root / 'notes/nested/a.md'
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('memo', encoding='utf-8')
    (artifacts.video_dir(VID) / "evidence.json").unlink()
    def must_not_load(*_):
        raise AssertionError("清理笔记不应读取课程产物")
    monkeypatch.setattr(workspace.context, "load_evidence", must_not_load)
    monkeypatch.setattr(workspace.outline_edits, "ensure", must_not_load)
    workspace.CourseWorkspace.clear_conversation(VID, "c1", course[2])
    assert not (ws.root / "notes").exists()
    assert (ws.root / "map.md").exists()


def test_write_note_creates_directly_but_only_proposes_for_existing(course):
    ws = build(course)
    assert ws.call('write_note', {'path': 'notes/新页.md', 'text': '草稿', 'base_revision': None})['success']
    item = notes.listing(VID)[0]
    assert item['agent_new'] and not item['has_proposal']
    assert notes.versions(VID, '新页.md')[0]['author'] == 'agent'
    note = notes.read(VID, '新页.md')
    result = ws.call('write_note', {'path': 'notes/新页.md', 'text': '草稿改', 'base_revision': note['revision']})
    assert result['success'] and '建议' in text(result)
    assert notes.read(VID, '新页.md')['text'] == '草稿'
    assert notes.listing(VID)[0]['has_proposal']
    same = ws.call('write_note', {'path': 'notes/新页.md', 'text': '草稿', 'base_revision': note['revision']})
    assert same['success'] and '无需' in text(same)
    # 已存在的名称不能再按「新建」覆盖
    assert not ws.call('write_note', {'path': 'notes/新页.md', 'text': '覆盖', 'base_revision': None})['success']


def test_patch_preserves_other_sections_and_pending_edits_with_explicit_status(course):
    ws = build(course)
    original = notes.save(VID, 'lesson.md', '开头保持\n公式错误\n结尾保持', None)
    args = {'path': 'notes/lesson.md', 'base_revision': original['revision'],
            'old_text': '公式错误', 'new_text': '公式正确'}
    result = json.loads(text(ws.call('patch_note', args)))
    assert result['ok'] and result['status'] == 'proposed' and result['proposal_id']
    state = json.loads(text(ws.call('read_nav', {'path': args['path'], 'metadata_only': True})))
    assert 'text' not in state and 'text' not in state['proposal']
    assert state['proposal']['id'] == result['proposal_id']
    assert notes.read(VID, 'lesson.md')['text'] == original['text']
    assert notes.proposal(VID, 'lesson.md')['text'] == '开头保持\n公式正确\n结尾保持'
    next_args = {**args, 'old_text': '结尾保持', 'new_text': '结尾补充'}
    assert json.loads(text(ws.call('patch_note', next_args)))['status'] == 'conflict'
    assert ws.call('patch_note', {**next_args, 'proposal_id': result['proposal_id']})['success']
    assert notes.proposal(VID, 'lesson.md')['text'] == '开头保持\n公式正确\n结尾补充'
    notes.save(VID, 'lesson.md', '用户的新内容', original['revision'])
    failure = json.loads(text(ws.call('patch_note', args)))
    assert not failure['ok'] and failure['status'] == 'conflict'
    assert notes.read(VID, 'lesson.md')['text'] == '用户的新内容'


def test_native_draft_submission_and_search_continuation(course):
    ws = build(course)
    draft = ws.root / 'draft.md'
    draft.write_text(r'公式 \(x^\top x\)', encoding='utf-8')
    submitted = ws.call('write_note_from_file', {'path': 'notes/native.md', 'draft_path': 'draft.md', 'base_revision': None})
    assert json.loads(text(submitted))['status'] == 'created'
    assert notes.read(VID, 'native.md')['text'] == draft.read_text(encoding='utf-8')
    assert not ws.call('write_note_from_file', {'path': 'notes/out.md', 'draft_path': '../secret'})['success']
    first = json.loads(text(ws.call('search', {'queries': ['normalization'], 'k': 2})))
    second = json.loads(text(ws.call('search', {'queries': ['normalization'], 'k': 2, 'offset': first['next_offset']})))
    assert first['next_offset'] == 2 and second['results']
    assert not ({r['id'] for r in first['results']} & {r['id'] for r in second['results']})



def test_write_note_rejects_swallowed_latex_backslashes_and_accepts_fullwidth(course):
    ws = build(course)

    def write(body, name='公式.md'):
        return ws.call('write_note', {'path': f'notes/{name}', 'text': body, 'base_revision': None})

    # 模型把单个反斜杠写进 JSON 参数后的典型残留：退格、换页、制表符、丢掉反斜杠的命令
    for broken in ('系数 (\x08eta_1)', 'x = \x0crac{a}{b}', 'a^\top b', '(a_1,ldots,a_m)',
                   '(sum_{i=1}^{m} a_i)', 'x \neq y'):
        result = write(broken)
        assert not result['success'], broken
        assert '全角' in text(result)
    assert notes.listing(VID) == []
    # 正确的 JSON 反斜杠、普通文字、代码里的普通制表符都不受影响
    assert write(r'行内 \(a_1,\ldots,a_m\)，$\frac{a}{b}$，求和 sum 与 frac 两个词', '好.md')['success']
    assert write('列\t列二', '表.md')['success']
    # 全角＼会被还原成真正的反斜杠
    assert write('＼(a_1,＼ldots,a_m＼)，＼beta，＼top', '全角.md')['success']
    assert notes.read(VID, '全角.md')['text'] == r'\(a_1,\ldots,a_m\)，\beta，\top'
