"""阶段 evidence（交接 §6）：segments.json + slides.json → evidence.json。纯本机组装，不调用模型，无可调参数。

产物 `{"pages", "sections", "speech"}`：
- pages：每页一条（`S012`），按首次出现排序。读图结果（标题、画面内容、状态）来自 slides.json，
  `image` / `sections` / `similar_prev` 来自 segments。
- sections：每个出现（`S012.k`）或无页段（`N001`），时间字段原样来自 segments；
  `spoken_summary`、`truncated`、`truncated_t_ids` 来自 slides.json 的出现记录（§5.3 截断透传）。
- speech：每段讲解（`T0031`），`category` 取所在页 slides.json 的 `speech_categories`；
  无页段的 T、读图失败或被跳过的页，category 为 null（无页 T 的分类在 outline.json，问答时合并读取）。

跳转时间（`jump`）：页取首次出现的 `display_start`；出现取 `display_start`，无页段取 `speech_start`；T 取自己的 start。

读图被跳过（slides.json 不存在，或 read_slides 在设置里被停用）时，页的 `status` 为 `"skipped"`，
标题、画面内容和摘要为空，category 为 null。读图失败的页（`status: "failed"`）同样保留，内容为空，下游用原文兜底。
"""
from __future__ import annotations

import re
from typing import Any

from .base import StageContext, StageError

# 可引用的证据 ID（交接 §6）：页 S012、出现 S012.2、无页段 N001、讲解片段 T0031。
# 不含捕获组和锚点，可直接拼进更大的正则（例如问答引用 `[S012.2, T0031]`）。
ID_PATTERN = r"(?:S\d{3,}(?:\.\d+)?|N\d{3,}|T\d{4,})"
ID_RE = re.compile(ID_PATTERN)
_ID_FULL_RE = re.compile(rf"^{ID_PATTERN}$")

CATEGORIES = ("lecture", "admin", "interaction", "media", "offtopic", "mixed", "uncertain")
PAGE_STATUSES = ("ok", "failed", "skipped")


def is_ref_id(s: str) -> bool:
    """s 是否整体符合可引用 ID 的格式（不检查是否存在，存在性用 id_index）。"""
    return bool(_ID_FULL_RE.match(s))


def fmt_time(t: float) -> str:
    """秒 → mm:ss 或 h:mm:ss，用于提示词与展示。"""
    t = max(0, int(round(t)))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- 组装

def _slide_records(slides: Any) -> dict[str, dict]:
    """slides.json → {页 ID: 记录}。兼容外层包一层 {"slides": [...]}。"""
    if isinstance(slides, dict):
        slides = slides.get("slides") or []
    return {r["id"]: r for r in slides or [] if isinstance(r, dict) and r.get("id")}


def _page_status(rec: dict | None) -> str:
    if rec is None:
        return "failed"          # slides.json 存在但缺这一页：读图没有结果，按失败处理
    st = rec.get("status")
    if st in ("ok", "failed"):
        return st
    return "ok" if (rec.get("visual_content") or "").strip() else "failed"


def _categories(rec: dict) -> dict[str, str]:
    """speech_categories → {T ID: category}。存储格式是字典（§5.4），也兼容模型返回的数组格式（§5.2）。"""
    raw = rec.get("speech_categories") or {}
    if isinstance(raw, list):
        raw = {c.get("id"): c.get("category") for c in raw if isinstance(c, dict)}
    return {k: v for k, v in raw.items() if k and v in CATEGORIES}


def _jump_section(sec: dict) -> float:
    keys = ("display_start", "speech_start") if sec.get("page") is not None else ("speech_start", "display_start")
    for k in keys:  # 第二个键只作防御：正常数据里第一个键总有值
        if sec.get(k) is not None:
            return float(sec[k])
    return 0.0


def build_evidence(segments: dict, slides: Any | None) -> dict:
    """组装 evidence.json 的内容。slides 为 None 表示读图被跳过。"""
    skipped = slides is None
    recs = {} if skipped else _slide_records(slides)
    seg_secs = segments.get("sections") or []
    seg_speech = segments.get("speech") or []

    # 页：按首次出现排序（segments 的 sections 已按时间排序）
    pages: dict[str, dict] = {}
    page_status: dict[str, str] = {}
    page_cats: dict[str, dict[str, str]] = {}
    occ_by_id: dict[str, dict] = {}
    for sec in seg_secs:
        pid = sec.get("page")
        if pid is None:
            continue
        if pid in pages:
            pages[pid]["sections"].append(sec["id"])
            continue
        rec = recs.get(pid)
        status = "skipped" if skipped else _page_status(rec)
        ok = status == "ok"
        page_status[pid] = status
        page_cats[pid] = _categories(rec) if ok and rec else {}
        if ok and rec:
            for occ in rec.get("occurrences") or []:
                if isinstance(occ, dict) and occ.get("id"):
                    occ_by_id[occ["id"]] = occ
        pages[pid] = {
            "id": pid,
            "title": (rec.get("title") or "") if ok and rec else "",
            "visual_content": (rec.get("visual_content") or "") if ok and rec else "",
            "status": status,
            "image": sec.get("image") or f"keyframes/{pid}.jpg",
            "sections": [sec["id"]],
            "similar_prev": sec.get("similar_prev"),
            "jump": _jump_section(sec),
        }

    sections = []
    for sec in seg_secs:
        pid = sec.get("page")
        occ = occ_by_id.get(sec["id"]) if pid is not None and page_status.get(pid) == "ok" else None
        t_ids = list(sec.get("t_ids") or [])
        trunc_ids = [t for t in (occ or {}).get("truncated_t_ids") or [] if t in t_ids]
        sections.append({
            "id": sec["id"], "page": pid,
            "display_start": sec.get("display_start"), "display_end": sec.get("display_end"),
            "speech_start": sec.get("speech_start"), "speech_end": sec.get("speech_end"),
            "jump": _jump_section(sec),
            "image": sec.get("image"),
            "spoken_summary": (occ or {}).get("spoken_summary") or "",
            "truncated": bool((occ or {}).get("truncated")) or bool(trunc_ids),
            "truncated_t_ids": trunc_ids,
            "t_ids": t_ids,
            "local_tags": list(sec.get("local_tags") or []),
        })

    sec_page = {s["id"]: s["page"] for s in sections}
    speech = []
    for t in seg_speech:
        pid = sec_page.get(t.get("section"))
        speech.append({
            "id": t["id"], "section": t.get("section"),
            "start": t["start"], "end": t["end"], "jump": float(t["start"]),
            "text": t.get("text") or "",
            "category": page_cats.get(pid, {}).get(t["id"]) if pid is not None else None,
            "cue_first": t.get("cue_first"), "cue_last": t.get("cue_last"),
        })

    return {"pages": list(pages.values()), "sections": sections, "speech": speech}


def id_index(evidence: dict) -> dict[str, dict]:
    """{ID: {"kind": "page"|"section"|"speech", "jump": 秒}}，供大纲和问答校验引用、映射跳转时间。"""
    idx: dict[str, dict] = {}
    for kind, key in (("page", "pages"), ("section", "sections"), ("speech", "speech")):
        for e in evidence.get(key) or []:
            idx[e["id"]] = {"kind": kind, "jump": float(e["jump"])}
    return idx


# ---------------------------------------------------------------- 阶段入口

def _read_slides(ctx: StageContext) -> Any | None:
    """读图结果；read_slides 在设置里被停用或 slides.json 不存在时返回 None（按跳过处理）。"""
    if "read_slides" in ctx.settings.disabled_stages:
        return None
    try:
        return ctx.read("slides.json")
    except FileNotFoundError:
        return None


def run(ctx: StageContext) -> None:
    try:
        segments = ctx.read("segments.json")
    except FileNotFoundError:
        raise StageError("缺少 segments.json，请先完成讲解片段切分") from None
    slides = _read_slides(ctx)
    ctx.progress(0.1, "组装证据")
    ev = build_evidence(segments or {}, slides)
    ctx.write("evidence.json", ev)

    pages = ev["pages"]
    n_fail = sum(1 for p in pages if p["status"] == "failed")
    n_none = sum(1 for s in ev["sections"] if s["page"] is None)
    msg = (f"证据：{len(pages)} 页、{len(ev['sections']) - n_none} 个出现、{n_none} 个无页段、"
           f"{len(ev['speech'])} 段讲解")
    if slides is None:
        msg += "；未读取幻灯片，摘要与分类为空"
    elif n_fail:
        msg += f"；{n_fail} 页读图失败，将用原文兜底"
    ctx.progress(1.0, msg)
