"""阶段 segments：cues.json + keyframes.json (+ transcript.json 的 language) → segments.json（交接 §4）。

纯本机计算，分三步：
1. 时间线：keyframes 的每次出现（appearance）是一个 section（`S012.k`）。相邻出现之间空档 < gap_absorb_s 时，
   只把前一段的**归属窗口**延到后一段开始；`display_start/end` 始终是真实显示区间，不做修改。
   即使出现期间一句话都没有，section 也保留（`t_ids: []`）。
2. 分配 cue：按中点时间归属 section；不在任何归属窗口内的连续 cue 组成无页段 `N001`…。
   换页落在句中（当前 section 末条 cue 不以句末标点结尾）、且新 cue 在边界后 ≤ sentence_snap_s 内结束时，
   这条 cue 留在当前 section。这只影响归属，不改显示时间。
3. 切 T：section 内累积 cue；cue 间静音 > silence_split_s 强制收口；时长 ≥ t_target_s 且在句末，
   或时长 ≥ t_max_s 时收口；末尾不足 t_min_tail_s 的尾巴并入前一个 T。T 按时间统一编号 `T0001`…，
   拼起来恰好覆盖全部 cue，每条一次、顺序不变。

无页段里出现非课程语言的文字时标 `local_tags: ["media_candidate"]`（按 Unicode 文字系统判断）。
`similar_prev`（keyframes §3.7）原样透传到该页的每个出现。
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from ..config import SegmentParams
from .base import StageContext, StageError

# 句末标点，允许后跟引号或右括号（交接 §4.2）
SENTENCE_END_RE = re.compile(r"[.?!。？！][\"'”’)）」』】\]]*$")

# 课程语言 → 该语言使用的文字系统（unicodedata 名称的首个单词）。
# 拉丁字母（术语、公式变量）和希腊字母（数学符号）在任何课程里都不算外语文字；未列出的语言按拉丁字母处理。
_ALWAYS_OK = {"LATIN", "GREEK"}
_LANG_SCRIPTS: dict[str, set[str]] = {
    "zh": {"CJK"}, "yue": {"CJK"},
    "ja": {"CJK", "HIRAGANA", "KATAKANA"},
    "ko": {"HANGUL", "CJK"},
    "ru": {"CYRILLIC"}, "uk": {"CYRILLIC"}, "be": {"CYRILLIC"}, "bg": {"CYRILLIC"},
    "sr": {"CYRILLIC"}, "mk": {"CYRILLIC"}, "kk": {"CYRILLIC"}, "mn": {"CYRILLIC"},
    "ar": {"ARABIC"}, "fa": {"ARABIC"}, "ur": {"ARABIC"},
    "he": {"HEBREW"}, "yi": {"HEBREW"},
    "th": {"THAI"},
    "hi": {"DEVANAGARI"}, "mr": {"DEVANAGARI"}, "ne": {"DEVANAGARI"},
    "el": {"GREEK"},
}
# 参与判断的文字系统；其余（全角字母、符号等）不计
_KNOWN_SCRIPTS = {"LATIN", "GREEK", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "CYRILLIC",
                  "ARABIC", "HEBREW", "THAI", "DEVANAGARI"}


def ends_sentence(text: str) -> bool:
    return bool(SENTENCE_END_RE.search(text.strip()))


def _script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        head = unicodedata.name(ch).split()[0]
    except ValueError:
        return None
    return head if head in _KNOWN_SCRIPTS else None


def _lang_key(language: str | None) -> str:
    return (language or "en").lower().replace("_", "-").split("-")[0]


def foreign_scripts(text: str, language: str | None) -> set[str]:
    """text 中出现的、不属于课程语言的文字系统。"""
    allowed = _ALWAYS_OK | _LANG_SCRIPTS.get(_lang_key(language), set())
    found = {s for s in map(_script, text) if s}
    return found - allowed


def _is_cjk_char(ch: str) -> bool:
    return _script(ch) in {"CJK", "HIRAGANA", "KATAKANA", "HANGUL"} or (
        "　" <= ch <= "〿" or "＀" <= ch <= "￯")


def join_texts(texts: list[str]) -> str:
    """拼接 cue 文本：两侧都是中日韩文字（含全角标点）时直接相连，否则用空格分隔。"""
    out = ""
    for t in (x.strip() for x in texts):
        if not t:
            continue
        if out and not (_is_cjk_char(out[-1]) and _is_cjk_char(t[0])):
            out += " "
        out += t
    return out


@dataclass
class _Section:
    page: str | None
    display_start: float | None = None
    display_end: float | None = None
    assign_start: float = 0.0
    assign_end: float = 0.0
    id: str = ""
    image: str | None = None
    similar_prev: Any = None
    cues: list[tuple[int, dict]] = field(default_factory=list)   # (cue 下标, cue)
    t_ids: list[str] = field(default_factory=list)
    local_tags: list[str] = field(default_factory=list)


def _timeline(keyframes: list[dict], p: SegmentParams) -> list[_Section]:
    """每次出现一个 section，按时间排序，并算好归属窗口。"""
    secs: list[_Section] = []
    for page in keyframes or []:
        imgs = page.get("appearance_images") or []
        # 出现与截图一一对应；按时间排序后编号，保证 S012.k 是第 k 次出现
        pairs = sorted(((float(a), float(b), imgs[i] if i < len(imgs) and imgs[i] else page.get("image"))
                        for i, (a, b) in enumerate(page.get("appearances") or [])),
                       key=lambda x: (x[0], x[1]))
        for k, (a, b, img) in enumerate(pairs, 1):
            secs.append(_Section(page=page["id"], display_start=a, display_end=b, assign_start=a,
                                 assign_end=b, id=f"{page['id']}.{k}", image=img,
                                 similar_prev=page.get("similar_prev")))
    secs.sort(key=lambda s: (s.display_start, s.display_end, s.page))
    for cur, nxt in zip(secs, secs[1:]):
        gap = nxt.display_start - cur.display_end
        if 0 < gap < p.gap_absorb_s:
            cur.assign_end = nxt.display_start   # 只延长归属窗口，不改显示时间
    return secs


def build_segments(cues: list[dict], keyframes: list[dict], params: SegmentParams,
                   language: str | None = "en") -> dict:
    """纯函数：返回 {"sections": [...], "speech": [...]}（交接 §4.3）。

    cues 按时间顺序（cues 阶段保证）；keyframes 为 keyframes.json 的 data（页列表），可为空。
    """
    p = params
    secs = _timeline(keyframes, p)
    starts = [s.assign_start for s in secs]
    reach: list[float] = []            # reach[i] = max(assign_end[0..i])，用于提前结束回溯
    for s in secs:
        reach.append(max(reach[-1], s.assign_end) if reach else s.assign_end)

    def sec_at(t: float) -> _Section | None:
        """归属窗口包含 t 的 section；有重叠时取开始最晚的。"""
        i = bisect_right(starts, t) - 1
        while i >= 0 and reach[i] > t:
            if t < secs[i].assign_end:
                return secs[i]
            i -= 1
        return None

    n_secs: list[_Section] = []
    cur: _Section | None = None
    for idx, c in enumerate(cues):
        target = sec_at((c["start"] + c["end"]) / 2)
        changing = cur is not None and target is not cur and not (target is None and cur.page is None)
        if changing and cur.cues and not ends_sentence(cur.cues[-1][1]["text"]):
            # 换页落在句中：新 cue 在边界后 ≤ sentence_snap_s 内结束，就留在当前 section
            bound = cur.assign_end if cur.page is not None else target.display_start
            if c["end"] - bound <= p.sentence_snap_s:
                cur.cues.append((idx, c))
                continue
        if target is None:
            if cur is None or cur.page is not None:
                cur = _Section(page=None)
                n_secs.append(cur)
        else:
            cur = target
        cur.cues.append((idx, c))

    for i, s in enumerate(n_secs, 1):
        s.id = f"N{i:03d}"
        if foreign_scripts(" ".join(c["text"] for _, c in s.cues), language):
            s.local_tags.append("media_candidate")
    all_secs = secs + n_secs

    # 切 T
    groups: list[tuple[_Section, list[tuple[int, dict]]]] = []
    for s in all_secs:
        s_groups: list[list[tuple[int, dict]]] = []
        buf: list[tuple[int, dict]] = []
        for idx, c in s.cues:
            # 静音硬切；cue 下标不连续（出现区间重叠时才会发生）也要切，保证 T 的 cue 区间不交叉
            if buf and (idx != buf[-1][0] + 1 or c["start"] - buf[-1][1]["end"] > p.silence_split_s):
                s_groups.append(buf)
                buf = []
            buf.append((idx, c))
            d = buf[-1][1]["end"] - buf[0][1]["start"]
            if (d >= p.t_target_s and ends_sentence(c["text"])) or d >= p.t_max_s:
                s_groups.append(buf)
                buf = []
        if buf:
            d = buf[-1][1]["end"] - buf[0][1]["start"]
            prev = s_groups[-1] if s_groups else None
            if (d < p.t_min_tail_s and prev and buf[0][0] == prev[-1][0] + 1
                    and buf[0][1]["start"] - prev[-1][1]["end"] <= p.silence_split_s):
                prev.extend(buf)
            else:
                s_groups.append(buf)
        groups += [(s, g) for g in s_groups]

    groups.sort(key=lambda x: x[1][0][0])
    speech: list[dict] = []
    for n, (s, g) in enumerate(groups, 1):
        t = {"id": f"T{n:04d}", "section": s.id, "start": g[0][1]["start"], "end": g[-1][1]["end"],
             "cue_first": g[0][1]["id"], "cue_last": g[-1][1]["id"],
             "text": join_texts([c["text"] for _, c in g])}
        speech.append(t)
        s.t_ids.append(t["id"])

    t_by = {t["id"]: t for t in speech}
    out_secs = []
    for s in all_secs:
        ts = [t_by[i] for i in s.t_ids]
        out_secs.append({
            "id": s.id, "page": s.page,
            "display_start": s.display_start, "display_end": s.display_end,
            "speech_start": min(t["start"] for t in ts) if ts else None,
            "speech_end": max(t["end"] for t in ts) if ts else None,
            "image": s.image, "t_ids": s.t_ids, "local_tags": s.local_tags,
            "similar_prev": s.similar_prev,
        })
    out_secs.sort(key=lambda s: (s["display_start"] if s["page"] is not None else s["speech_start"],
                                 s["page"] is None))
    return {"sections": out_secs, "speech": speech}


def run(ctx: StageContext) -> None:
    try:
        cues = ctx.read("cues.json") or []
    except FileNotFoundError:
        raise StageError("找不到 cues.json，请先完成字幕切分阶段") from None
    notes: list[str] = []
    try:
        keyframes = ctx.read("keyframes.json") or []
    except FileNotFoundError:
        keyframes = []
        notes.append("没有抽帧结果，全部按无幻灯片处理")
    try:
        language = (ctx.read("transcript.json") or {}).get("language")
    except FileNotFoundError:
        language = None
    if not language:
        language = "en"
        notes.append("未找到课程语言，按英文处理")

    data = build_segments(cues, keyframes, ctx.settings.segment, language)
    ctx.write("segments.json", data)

    secs = data["sections"]
    n_page = sum(1 for s in secs if s["page"] is not None)
    n_none = len(secs) - n_page
    msg = f"切分出 {n_page} 个出现、{n_none} 个无页段、{len(data['speech'])} 段讲解"
    media = sum(1 for s in secs if "media_candidate" in s["local_tags"])
    if media:
        msg += f"；{media} 个无页段可能是影片"
    ctx.progress(1.0, "；".join([msg, *notes]))
