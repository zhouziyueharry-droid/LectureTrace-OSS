"""Shared evidence queries; no workspace, note migration or read-log writes."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict

from .. import artifacts
from ..pipeline.evidence import fmt_time, id_index
from . import context, outline_edits


class WorkspaceError(ValueError):
    pass


def _terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold())
    return [term for word in words for term in
            ([word] if not re.fullmatch(r"[\u4e00-\u9fff]{2,}", word)
             else [word[i:i + 2] for i in range(len(word) - 1)])]

class EvidenceReader:
    def __init__(self, video_id, evidence, outline, options):
        self.video_id, self.evidence, self.outline, self.options = video_id, evidence, outline, options
        self.version = "sha256:" + outline_edits.fingerprint(evidence)
        self.index = id_index(evidence)
        self.records = {r["id"]: r for key in ("pages", "sections", "speech") for r in evidence.get(key) or []}
        self.pages = {p["id"]: p for p in evidence.get("pages") or []}
        self.sections = {s["id"]: s for s in evidence.get("sections") or []}
        chapters = sorted(enumerate(outline.get("chapters") or [], 1), key=lambda pair: pair[1].get("start", 0))
        self.chapter_for = {}
        for ev_id, rec in self.records.items():
            eligible = [n for n, ch in chapters if float(ch.get("start", 0)) <= float(rec.get("jump", 0))]
            self.chapter_for[ev_id] = eligible[-1] if eligible else 0
        self._search_index()

    def _check_version(self):
        if "sha256:" + outline_edits.evidence_version(self.video_id) != self.version:
            raise WorkspaceError("课程证据已经变化，请重新读取目录")

    def _finish(self, text, ids=None, image=None):
        self._check_version()
        return {"text": text, "ids": ids or [], "image": image}

    def _end(self, section: dict) -> float:
        return max(float(section.get(k) or section["jump"]) for k in ("display_end", "speech_end"))

    def _search_index(self):
        self.documents = {}
        chapters = self.outline.get("chapters") or []
        for ev_id, record in self.records.items():
            chapter = self.chapter_for[ev_id]
            ch = chapters[chapter - 1] if chapter else {}
            outline_text = json.dumps(ch, ensure_ascii=False)
            text = " ".join(str(record.get(k) or "") for k in ("title", "visual_content", "spoken_summary", "text"))
            self.documents[ev_id] = {"text": text.strip(), "terms": Counter(_terms(text + " " + outline_text))}
        self.df = Counter(term for doc in self.documents.values() for term in doc["terms"])
        self.avg_length = (sum(sum(d["terms"].values()) for d in self.documents.values())
                           / max(1, len(self.documents))) or 1

    def _search(self, args: dict) -> dict:
        queries = args.get("queries")
        k = args.get("k", self.options["search_k"])
        if not isinstance(queries, list) or not queries or not all(isinstance(q, str) for q in queries):
            raise WorkspaceError("queries 必须是非空文字数组")
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise WorkspaceError("k 必须是正整数")
        offset = args.get('offset', 0)
        if type(offset) is not int or offset < 0:
            raise WorkspaceError('offset 必须是非负整数')
        chapter = args.get("chapter")
        if chapter is not None:
            matches = [n for n, ch in enumerate(self.outline.get("chapters") or [], 1)
                       if str(chapter) in (str(n), f"{n:02d}", ch["node_id"])]
            if not matches:
                raise WorkspaceError("章节不存在")
            chapter = matches[0]
        query_terms = set(term for q in queries for term in _terms(q))
        scores = []
        for ev_id, doc in self.documents.items():
            if chapter is not None and self.chapter_for[ev_id] != chapter:
                continue
            length = sum(doc["terms"].values())
            score = 0.0
            for term in query_terms:
                freq = doc["terms"].get(term, 0)
                if freq:
                    weight = math.log(1 + (len(self.documents) - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    score += weight * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * length / self.avg_length))
            if score:
                scores.append((score, ev_id))
        groups = defaultdict(list)
        for score, ev_id in sorted(scores, key=lambda row: (-row[0], self.index[row[1]]["jump"], row[1])):
            rec = self.records[ev_id]
            sec = self.sections.get(rec.get("section")) or (rec if ev_id in self.sections else {})
            page = sec.get("page") or (ev_id if ev_id in self.pages else sec.get("id", ev_id))
            groups[(self.chapter_for[ev_id], page)].append(ev_id)
        selected = []
        while len(selected) < offset + k and any(groups.values()):
            for group in groups.values():
                if group and len(selected) < offset + k:
                    selected.append(group.pop(0))
        results = []
        for ev_id in selected[offset:]:
            text = self.documents[ev_id]["text"]
            positions = [text.casefold().find(t) for t in query_terms if t in text.casefold()]
            start = max(0, min(positions) - self.options["snippet_chars"] // 4) if positions else 0
            results.append({"id": ev_id, "start": self.index[ev_id]["jump"],
                            "chapter": self.chapter_for[ev_id], "excerpt": text[start:start + self.options["snippet_chars"]]})
        next_offset = offset + len(results)
        return self._finish(json.dumps({"results": results, 'total': len(scores),
                                       'next_offset': next_offset if next_offset < len(scores) else None,
                                       "note": "摘录仅定位，要引用请先 read_evidence；可用 next_offset 继续查看命中"}, ensure_ascii=False))

    def _read(self, raw_ids) -> dict:
        if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(s, str) for s in raw_ids):
            raise WorkspaceError("ids 必须是非空 ID 数组")
        ids = []
        for value in raw_ids:
            match = re.fullmatch(r"(T\d{4,})-(T\d{4,})", value)
            if match:
                a, b = match.groups()
                if a not in self.index or b not in self.index or int(a[1:]) > int(b[1:]):
                    raise WorkspaceError("证据区间无效")
                ids.extend(i for i in self.index if i.startswith("T") and int(a[1:]) <= int(i[1:]) <= int(b[1:]))
            elif value in self.index:
                ids.append(value)
            else:
                raise WorkspaceError("证据 ID 不存在")
        ids = list(dict.fromkeys(ids))
        body = []
        for ev_id in ids:
            rec = self.records[ev_id]
            kind = self.index[ev_id]["kind"]
            text = rec.get({"page": "visual_content", "section": "spoken_summary", "speech": "text"}[kind]) or ""
            source = {'page': '画面识别文字（公式/图示请核对原图）', 'section': '生成的讲解摘要（重要论断请核对 T 片段）',
                      'speech': '课堂语音转录（可能有识别错误）'}[kind]
            body.append(f"[{ev_id}] {fmt_time(rec['jump'])} · {source}\n{text or '（没有正文，请读取所属讲解片段或截图）'}")
        actual = [i for i in ids if self.records[i].get({"page": "visual_content", "section": "spoken_summary", "speech": "text"}[self.index[i]["kind"]])]
        return self._finish("\n\n".join(body), actual)

    def _image(self, ev_id) -> dict:
        if not isinstance(ev_id, str) or ev_id not in self.index or self.index[ev_id]["kind"] not in ("page", "section"):
            raise WorkspaceError("截图 ID 无效")
        rec = self.records[ev_id]
        rel = rec.get("image") or (self.pages.get(rec.get("page")) or {}).get("image")
        if not isinstance(rel, str):
            raise WorkspaceError("该证据没有截图")
        video_root = artifacts.video_dir(self.video_id).resolve()
        path = (video_root / rel).resolve()
        if not path.is_relative_to(video_root) or not path.is_file():
            raise WorkspaceError("截图不存在或路径无效")
        return self._finish(f"[{ev_id}] {fmt_time(rec['jump'])} 幻灯片截图", [ev_id],
                            "data:image/jpeg;base64," + context._encode(path))
