"""阶段 read_slides（提案 0001 交接 §5）：segments.json + keyframes.json → slides.json。

请求内容（§5.1）：按页分批（`SlideParams.batch_size`），每页依次放入
  页 ID → 页截图 → 该页每次出现（第 2 次起附当次截图 `appearance_images[k-1]`）→ 该出现的讲解片段 T（带 ID 与时间）。
每个出现的讲解按 `max_speech_chars` 以**整段 T** 截断，被截掉的 T 在请求里写明并要求标 uncertain，
产物的出现记录带 `truncated` / `truncated_t_ids`。无页段（N001…）的 T 不进读图请求（由大纲分类，§7）。

输出（§5.2）用结构化输出 `llm.complete_structured`：每批用 `schemas.slides_response_model` 把页 / 出现 / T 的 ID
限定为本批输入的枚举，再用 check 检查每个出现、每个 T 恰好一次、`visual_content` 非空（`title` 允许为空）。

失败处理（§5.3）：
- 整批通过 → 各页 ok；
- 重试后仍不合格（LLMInvalidOutputError）→ 从最后一次回复里逐页挑出合格的记录，其余页记为失败；
- 拒答 / 截断（LLMRefusalError / LLMTruncatedError）→ 本批各页记为失败；
- 多页批次中有失败页时，把失败页拆成单页各重试一次（SlideParams.split_retry）；仍失败的页 `status: "failed"`、字段留空；
- 其他 LLMError（网络、Key、限流）→ 阶段失败（StageError），已完成的页在断点缓存里，重试时从断点继续。
阶段本身不因个别页失败而失败，进度消息列出失败数。

断点缓存（§5.3.1）：`slides.partial.json` 只在 schema_version、缓存格式、params 都一致时复用；
每条记录再用 Pydantic 模型校验，并比对本页请求内容的指纹（讲解、截图、提示词、课程语言），不一致就重读。
只缓存成功的页；失败页在续跑时会重新读取。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import artifacts
from ..config import SCHEMA_VERSION, SlideParams
from ..llm import client as llm, schemas, settings as llm_settings
from .base import StageContext, StageError
from .evidence import fmt_time

log = logging.getLogger(__name__)

PARTIAL = "slides.partial.json"
PARTIAL_FORMAT = 2          # 断点缓存的记录格式；v1（bullets / formulas …）的缓存一律丢弃
MAX_OUTPUT_TOKENS = 16000   # 含推理 token
DEFAULT_LANGUAGE = "en"

# 以 v2 实验（2026-09-27，Luna / Sol 实测）的 SYSTEM_V2 为准；改动见交付说明：
# 补了空标题、后续出现的手写标注 [handwritten @S012.2]、truncated 的说明，以及课程语言一行（system_prompt）。
SYSTEM = """You are reading the slides of a recorded lecture. For each slide page you get its page ID, a screenshot,
and the lecturer's speech for every occurrence of that page. Each speech segment has an ID (T####) and a time range.
If a page was shown more than once, later occurrences may come with their own screenshot.

Return one record per page:
- title: the slide title exactly as written. If no title is visible, leave it empty; never invent one.
- visual_content: everything visible on the slide in reading order, as Markdown, based on the page screenshot.
  Formulas in LaTeX ($...$ / $$...$$). Describe figures and tables briefly. Transcribe handwritten annotations
  and mark them [handwritten]; if unreadable write [handwritten, illegible]. Handwriting that only appears in a
  later occurrence's screenshot goes here too, marked with that occurrence ID, e.g. [handwritten @S012.2]. Never guess.
- occurrences: for every occurrence ID given, a spoken_summary of what the lecturer said during that occurrence,
  based only on the given speech. Points not on the slide are the most valuable.
  If an occurrence has no speech, give an empty spoken_summary.
- speech_categories: every T ID given, exactly once, with one category:
  lecture (teaching content) | admin (assignments, exams, attendance, schedule) |
  interaction (Q&A with students, polls, waiting for answers) | media (playing a video or audio clip) |
  offtopic (unrelated chatter) | mixed (genuinely mixes teaching and non-teaching) |
  uncertain (cannot tell, including speech marked as truncated).
  Speech marked as truncated is not shown: categorise it as uncertain and do not summarise it.
Write every field in the language of the lecture. Use only IDs that appear in the input."""


# 课程语言代码（transcript.json 的 language）→ 写进提示词的语言名。未列出的代码直接用代码本身。
# 必须写出具体语言名：v2 实验中只写 "the language of the lecture" 时，Sol 把英语课 S091–S095 的摘要写成了西班牙语。
LANGUAGE_NAMES = {
    "en": "English", "zh": "Chinese", "yue": "Cantonese", "ja": "Japanese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "ru": "Russian", "uk": "Ukrainian", "pl": "Polish", "cs": "Czech", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "fi": "Finnish", "el": "Greek", "tr": "Turkish", "ar": "Arabic", "fa": "Persian",
    "he": "Hebrew", "hi": "Hindi", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
}


def language_code(language: str | None) -> str:
    return ((language or DEFAULT_LANGUAGE).strip().lower().replace("_", "-").split("-")[0]) or DEFAULT_LANGUAGE


def language_name(language: str | None) -> str:
    code = language_code(language)
    return LANGUAGE_NAMES.get(code, code)


def system_prompt(language: str | None) -> str:
    name = language_name(language)
    return (f"{SYSTEM}\nThe lecture is in {name}. Write title, visual_content and spoken_summary in {name} "
            "(transcribe slide text as written; do not translate).")


# ---------- 输出语言的本地检查（软检查，只对英语课程） ----------
# 阈值来源：v2 实验 docs/proposals/0001-experiments/v2_exp/result_v2.json（2026-09-27，S086–S095，Luna + Sol）：
# - 英文 spoken_summary 中英语虚词（the / of / is …）占词数 0.30–0.45（15 条）；Sol 误写成西班牙语的
#   S091.1–S095.1 为 0.000–0.024。取 0.10，两边都有很大余量。visual_content 是要点式转录，英文也可低至 0.05，不做此项检查。
# - 非 ASCII 字母比例在西班牙语摘要里只有 0.5%–2%，与带 "Schrödinger" 之类人名的英文分不开，所以只用它抓
#   中文 / 西里尔等其他文字：摘要 > 0.20、画面内容 > 0.50 才算（幻灯片本身可能夹带其他文字，阈值放宽）。
# - 只统计字母：公式（$…$、LaTeX 命令）先去掉；希腊字母、ℝ 等数学符号、引号、破折号、数字都不计。
# - 文字少于 lang_min_words 个词 / lang_min_letters 个字母时不判断。
# 阈值在 config.SlideParams（lang_*），参数变化会触发 read_slides 重跑。
EN_FUNCTION_WORDS = frozenset(
    "the a an of to and or in on at for with by from is are was were be been being it its this that these those "
    "as he she they we you his her their our not but if then so than which who what when how can will would "
    "should may might does do did has have had into about over also there here".split())
_SCRIPTS = {"LATIN", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "CYRILLIC", "ARABIC", "HEBREW", "THAI", "DEVANAGARI"}
_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$|\\\(.*?\\\)|\\\[.*?\\\]|\\[A-Za-z]+", re.S)
_WORD_RE = re.compile(r"[^\W\d_]+")


def _foreign_letter_ratio(text: str, min_letters: int) -> float | None:
    """非英语字母（带重音的拉丁字母、其他文字）占字母的比例；字母太少返回 None。希腊字母等数学符号不计。"""
    total = foreign = 0
    for ch in _MATH_RE.sub(" ", text):
        if not ch.isalpha():
            continue
        try:
            script = unicodedata.name(ch).split()[0]
        except ValueError:
            continue
        if script not in _SCRIPTS:
            continue
        total += 1
        foreign += script != "LATIN" or ord(ch) > 127
    return foreign / total if total >= min_letters else None


def _function_word_ratio(text: str, min_words: int) -> float | None:
    words = [w.lower() for w in _WORD_RE.findall(_MATH_RE.sub(" ", text))]
    if len(words) < min_words:
        return None
    return sum(w in EN_FUNCTION_WORDS for w in words) / len(words)


def language_problems(resp, language: str | None, p: SlideParams | None = None) -> list[str]:
    """英语课程中，明显不是英语的 spoken_summary / visual_content（英文，用作重试反馈）。其他语言不检查。"""
    if language_code(language) != "en":
        return []
    p = p or SlideParams()
    problems = []
    for r in resp.slides:
        ratio = _foreign_letter_ratio(r.visual_content, p.lang_min_letters)
        if ratio is not None and ratio > p.lang_max_foreign_visual:
            problems.append(f"{r.id}: visual_content does not look like English; the lecture is in English. "
                            "Transcribe slide text as written and write descriptions in English.")
        for o in r.occurrences:
            fw = _function_word_ratio(o.spoken_summary, p.lang_min_words)
            fl = _foreign_letter_ratio(o.spoken_summary, p.lang_min_letters)
            if (fw is not None and fw < p.lang_min_function_words) or (fl is not None and fl > p.lang_max_foreign_summary):
                problems.append(f"{r.id}: spoken_summary of {o.id} is not written in English; "
                                "the lecture is in English, write it in English.")
    return problems


def encode_image(path: Path, max_edge: int) -> str:
    """缩到长边 max_edge，转 JPEG，返回 base64。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------- 请求内容 ----------

@dataclass
class PagePlan:
    """一页的读图请求片段。parts 里的图片是 {"type": "image", "path": 相对路径}，发送前才编码。"""
    id: str
    appearances: list
    parts: list[dict]
    occurrence_ids: list[str]
    t_ids: list[str]                                   # 本页全部 T（含被截掉的），按出现与时间顺序
    truncated: dict[str, list[str]] = field(default_factory=dict)   # 出现 ID → 被截掉的 T
    input_hash: str = ""


def _occurrence_k(occ_id: str) -> int:
    try:
        return int(occ_id.rsplit(".", 1)[1])
    except (IndexError, ValueError):
        return 1


def course_language(ctx: StageContext) -> str:
    try:
        lang = (ctx.read("transcript.json") or {}).get("language")
    except (FileNotFoundError, AttributeError, ValueError):
        lang = None
    return str(lang).strip() if lang else DEFAULT_LANGUAGE


def _own_screenshot(sec: dict, kf: dict, k: int) -> str | None:
    """第 k（≥2）次出现的当次截图；与页截图相同（旧抽帧结果没有当次截图）时返回 None。"""
    imgs = kf.get("appearance_images") or []
    cand = sec.get("image") or (imgs[k - 1] if len(imgs) >= k else None)
    return cand if cand and cand != kf.get("image") else None


def speech_lines(t_ids: list[str], speech: dict[str, dict], max_chars: int) -> tuple[list[str], list[str]]:
    """出现内的 T 逐行列出；累计字符超过 max_chars 后，剩下的 T 整条不放（不切半条）。返回 (行, 被截掉的 T)。"""
    lines, cut, used = [], [], 0
    for tid in t_ids:
        t = speech[tid]
        line = f"  [{tid} {fmt_time(t['start'])}–{fmt_time(t['end'])}] {(t.get('text') or '').strip()}"
        if cut or used + len(line) > max_chars:
            cut.append(tid)
            continue
        used += len(line)
        lines.append(line)
    return lines, cut


def page_plan(vdir: Path, kf: dict, secs: list[dict], speech: dict[str, dict], max_chars: int,
              system: str) -> PagePlan:
    pid = kf["id"]
    if not kf.get("image") or not (vdir / kf["image"]).exists():
        raise StageError(f"缺少关键帧图片 {kf.get('image') or pid}")
    parts: list[dict] = [{"type": "text", "text": f"[{pid}] screenshot:"},
                         {"type": "image", "path": kf["image"]}]
    truncated: dict[str, list[str]] = {}
    t_all: list[str] = []
    for sec in secs:
        k = _occurrence_k(sec["id"])
        span = (f"{fmt_time(sec['display_start'])}–{fmt_time(sec['display_end'])}"
                if sec.get("display_start") is not None and sec.get("display_end") is not None else "?")
        head = f"Occurrence {sec['id']} ({span})"
        shot = _own_screenshot(sec, kf, k) if k > 1 else None
        if shot and not (vdir / shot).exists():
            log.warning("出现 %s 的当次截图 %s 不存在，只发页截图", sec["id"], shot)
            shot = None
        lines: list[str] = []
        if shot:
            parts.append({"type": "text", "text": head + ", screenshot:"})
            parts.append({"type": "image", "path": shot})
        else:
            lines.append(head + ":")
        t_ids = list(sec.get("t_ids") or [])
        missing = [t for t in t_ids if t not in speech]
        if missing:
            raise StageError(f"segments.json 不完整：出现 {sec['id']} 引用的讲解片段 {', '.join(missing)} 不存在，"
                             "请重新运行讲解片段切分")
        shown, cut = speech_lines(t_ids, speech, max_chars)
        lines += shown
        if not t_ids:
            lines.append("  (no speech)")
        if cut:
            lines.append(f"  (… truncated: {', '.join(cut)} not shown — categorise them as uncertain)")
            truncated[sec["id"]] = cut
        parts.append({"type": "text", "text": "\n".join(lines)})
        t_all += t_ids
    # 指纹：请求文字 + 截图文件（路径与大小）+ 提示词（含课程语言）。任何一项变了，断点缓存就不能复用
    fp = [system] + [p["text"] if p["type"] == "text" else [p["path"], _size(vdir / p["path"])] for p in parts]
    digest = hashlib.sha256(json.dumps(fp, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return PagePlan(id=pid, appearances=kf.get("appearances") or [], parts=parts,
                    occurrence_ids=[s["id"] for s in secs], t_ids=t_all, truncated=truncated, input_hash=digest)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def load_plans(ctx: StageContext, keyframes: list[dict] | None = None) -> tuple[str, list[PagePlan]]:
    """读 keyframes / segments / transcript，返回 (课程语言代码, 每页的请求片段)。供 run 与发送前估算共用。"""
    if keyframes is None:
        try:
            keyframes = ctx.read("keyframes.json")
        except FileNotFoundError as e:
            raise StageError("缺少 keyframes.json，请先完成关键帧提取") from e
    language = course_language(ctx)
    if not keyframes:
        return language, []
    try:
        seg = ctx.read("segments.json")
    except FileNotFoundError as e:
        raise StageError("缺少 segments.json，请先完成讲解片段切分") from e
    system = system_prompt(language)
    speech = {t["id"]: t for t in seg.get("speech") or []}
    by_page: dict[str, list[dict]] = defaultdict(list)
    for sec in seg.get("sections") or []:
        if sec.get("page"):
            by_page[sec["page"]].append(sec)
    kf_ids = {kf["id"] for kf in keyframes}
    stray = sorted(set(by_page) - kf_ids)
    lonely = [kf["id"] for kf in keyframes if kf["id"] not in by_page]
    if stray or lonely:
        detail = "；".join(x for x in (stray and f"segments 中的页 {', '.join(stray)} 不在 keyframes 中",
                                       lonely and f"页 {', '.join(lonely)} 没有出现记录") if x)
        raise StageError(f"segments.json 与 keyframes.json 不一致（{detail}），请重新运行讲解片段切分")
    max_chars = ctx.settings.slides.max_speech_chars
    plans = []
    for kf in keyframes:
        secs = sorted(by_page[kf["id"]], key=lambda s: _occurrence_k(s["id"]))
        plans.append(page_plan(ctx.dir, kf, secs, speech, max_chars, system))
    return language, plans


def build_batch_content(ctx: StageContext, batch: list[PagePlan], language: str | None = None) -> list[dict]:
    """一批的消息内容（Anthropic 风格内容块，图片已编码）。"""
    max_edge = ctx.settings.slides.max_edge
    content: list[dict] = [{"type": "text", "text": f"This batch has {len(batch)} page{'s' if len(batch) > 1 else ''}."}]
    for plan in batch:
        for p in plan.parts:
            if p["type"] == "image":
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": encode_image(ctx.dir / p["path"], max_edge)}})
            else:
                content.append({"type": "text", "text": p["text"]})
    ids = ", ".join(p.id for p in batch)
    content.append({"type": "text", "text": f"Return {{\"slides\": [...]}} with exactly one record for each of {ids}. "
                                            f"Write all text fields in {language_name(language)}."})
    return content


def batch_contents(ctx: StageContext, keyframes: list[dict] | None = None) -> tuple[str, list[list[dict]]]:
    """发送前估算用：(系统提示, 每批的消息内容)，与 run 实际发送的内容一致（不含失败后的重试）。"""
    language, plans = load_plans(ctx, keyframes)
    size = max(1, ctx.settings.slides.batch_size)
    return system_prompt(language), [build_batch_content(ctx, plans[i:i + size], language)
                                     for i in range(0, len(plans), size)]


# ---------- 校验 ----------

def _page_problems(rec, plan: PagePlan) -> list[str]:
    """一页记录的问题（英文，用作重试反馈）：出现齐全且各一次、T 各一次、visual_content 非空。"""
    pid, problems = plan.id, []
    got = [o.id for o in rec.occurrences]
    missing = [o for o in plan.occurrence_ids if o not in got]
    extra = sorted({o for o in got if o not in plan.occurrence_ids})
    dup = sorted({o for o in got if got.count(o) > 1})
    if missing:
        problems.append(f"{pid}: occurrences is missing {', '.join(missing)}")
    if extra:
        problems.append(f"{pid}: occurrences contains {', '.join(extra)}, which belong to other pages")
    if dup:
        problems.append(f"{pid}: occurrences lists {', '.join(dup)} more than once")
    problems += [f"{pid}: {p}" for p in schemas.check_speech_categories(rec.speech_categories, plan.t_ids)]
    if not rec.visual_content.strip():
        problems.append(f"{pid}: visual_content is empty; transcribe what is visible on the slide")
    return problems


def _batch_problems(resp, batch: list[PagePlan]) -> list[str]:
    recs: dict[str, list] = defaultdict(list)
    for r in resp.slides:
        recs[r.id].append(r)
    problems = []
    for plan in batch:
        rs = recs.get(plan.id, [])
        if not rs:
            problems.append(f"missing the record for page {plan.id}")
        elif len(rs) > 1:
            problems.append(f"page {plan.id} has {len(rs)} records; give exactly one")
        else:
            problems += _page_problems(rs[0], plan)
    return problems


def _salvage(raw_text: str, model: type[schemas.SlidesResponse], batch: list[PagePlan]) -> dict[str, object]:
    """整批不合格时，从最后一次回复里逐页挑出合格的记录。"""
    try:
        data = llm.parse_json(raw_text or "")
    except ValueError:
        return {}
    items = data.get("slides") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    record_model = get_args(model.model_fields["slides"].annotation)[0]
    recs: dict[str, list] = defaultdict(list)
    for it in items:
        try:
            r = record_model.model_validate(it)
        except ValidationError:
            continue
        recs[r.id].append(r)
    plans = {p.id: p for p in batch}
    return {pid: rs[0] for pid, rs in recs.items()
            if pid in plans and len(rs) == 1 and not _page_problems(rs[0], plans[pid])}


# ---------- 调用 ----------

def _make_check(batch: list[PagePlan], language: str, p: SlideParams):
    """complete_structured 的 check：结构问题每次都报；语言问题是软检查，只在第一次发现时报一次
    （触发一次带反馈的重试），之后只记警告，不会因为语言让页失败（幻灯片本身可能夹带其他文字）。"""
    state = {"lang_reported": False}

    def check(resp) -> list[str]:
        problems = _batch_problems(resp, batch)
        lang = language_problems(resp, language, p)
        if lang and not state["lang_reported"]:
            state["lang_reported"] = True
            return problems + lang
        if lang:
            log.warning("幻灯片输出仍不是课程语言（已带说明重试过，接受）：%s", "; ".join(lang[:3]))
        return problems

    return check


def _call(ctx: StageContext, language: str, batch: list[PagePlan]) -> tuple[dict[str, object], dict[str, str]]:
    """一次结构化调用（内含带说明的重试）。返回 (合格记录, 失败页 → 原因)。"""
    model = schemas.slides_response_model([p.id for p in batch],
                                          [o for p in batch for o in p.occurrence_ids],
                                          [t for p in batch for t in p.t_ids])
    content = build_batch_content(ctx, batch, language)
    try:
        resp = llm.complete_structured(
            messages=[{"role": "user", "content": content}], system=system_prompt(language), response_model=model,
            max_tokens=MAX_OUTPUT_TOKENS, video_id=ctx.video_id, stage="read_slides",
            effort=llm_settings.effort_for("read_slides"), check=_make_check(batch, language, ctx.settings.slides))
    except (llm.LLMRefusalError, llm.LLMTruncatedError) as e:
        return {}, {p.id: str(e) for p in batch}
    except llm.LLMInvalidOutputError as e:
        ok = _salvage(e.raw_text, model, batch)
        reason = "；".join(e.problems[:3]) or str(e)
        return ok, {p.id: reason for p in batch if p.id not in ok}
    except llm.LLMError as e:
        raise StageError(f"读取幻灯片 {batch[0].id}–{batch[-1].id} 失败：{e}（已完成的页已保存，重试会从断点继续）") from e
    return {r.id: r for r in resp.slides}, {}


def read_batch(ctx: StageContext, language: str, batch: list[PagePlan]) -> tuple[dict[str, dict], dict[str, str]]:
    """读一批；多页批次中失败的页拆成单页各重试一次。返回 (成功页 → 记录, 失败页 → 原因)。"""
    ok, failed = _call(ctx, language, batch)
    if failed and len(batch) > 1 and ctx.settings.slides.split_retry:
        log.warning("幻灯片 %s 读取失败，拆成单页重试", "、".join(failed))
        for plan in [p for p in batch if p.id in failed]:
            got, bad = _call(ctx, language, [plan])
            ok.update(got)
            failed.pop(plan.id, None)
            failed.update(bad)
    by_id = {p.id: p for p in batch}
    for pid, why in failed.items():
        log.warning("幻灯片 %s 读取失败：%s", pid, why)
    return {pid: ok_record(by_id[pid], rec) for pid, rec in ok.items()}, failed


# ---------- 产物记录（§5.4） ----------

def _occurrences(plan: PagePlan, summaries: dict[str, str] | None) -> list[dict]:
    return [{"id": o, "spoken_summary": (summaries or {}).get(o, ""),
             "truncated": bool(plan.truncated.get(o)), "truncated_t_ids": list(plan.truncated.get(o, []))}
            for o in plan.occurrence_ids]


def ok_record(plan: PagePlan, rec) -> dict:
    cats = schemas.speech_categories_dict(rec.speech_categories)
    for cut in plan.truncated.values():   # 模型没看到被截掉的 T，一律记为 uncertain（§5.1）
        for t in cut:
            cats[t] = "uncertain"
    return {"id": plan.id, "status": "ok", "title": rec.title.strip(), "visual_content": rec.visual_content.strip(),
            "occurrences": _occurrences(plan, {o.id: o.spoken_summary.strip() for o in rec.occurrences}),
            "speech_categories": {t: cats[t] for t in plan.t_ids}}


def failed_record(plan: PagePlan) -> dict:
    return {"id": plan.id, "status": "failed", "title": "", "visual_content": "",
            "occurrences": _occurrences(plan, None), "speech_categories": {}}


# ---------- 断点缓存（§5.3.1） ----------

class _CachedOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    spoken_summary: str
    truncated: bool
    truncated_t_ids: list[str]


class _CachedSlide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: Literal["ok"]
    title: str
    visual_content: str = Field(min_length=1)
    occurrences: list[_CachedOccurrence]
    speech_categories: dict[str, schemas.Category]
    input_hash: str


def _cached_ok(item, plan: PagePlan) -> bool:
    try:
        c = _CachedSlide.model_validate(item)
    except ValidationError:
        return False
    return (c.id == plan.id and c.input_hash == plan.input_hash and bool(c.visual_content.strip())
            and [o.id for o in c.occurrences] == plan.occurrence_ids
            and list(c.speech_categories) == plan.t_ids
            and all(o.truncated_t_ids == plan.truncated.get(o.id, []) for o in c.occurrences))


def _partial_header(ctx: StageContext) -> dict:
    return {"schema_version": SCHEMA_VERSION, "format": PARTIAL_FORMAT, "params": ctx.params}


def load_partial(ctx: StageContext, plans: list[PagePlan]) -> dict[str, dict]:
    """可复用的成功记录（不含 input_hash）。头部不一致时整份丢弃；逐条校验不通过的丢弃。"""
    path = ctx.dir / PARTIAL
    if not path.exists():
        return {}
    try:
        obj = artifacts.read_json(path)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(obj, dict) or any(obj.get(k) != v for k, v in _partial_header(ctx).items()):
        log.info("slides.partial.json 的版本或参数与当前不一致，丢弃")
        return {}
    items = obj.get("items") if isinstance(obj.get("items"), dict) else {}
    done = {}
    for plan in plans:
        item = items.get(plan.id)
        if item is None:
            continue
        if _cached_ok(item, plan):
            done[plan.id] = {k: v for k, v in item.items() if k != "input_hash"}
        else:
            log.info("断点缓存中的 %s 与当前输入不一致或格式不对，重新读取", plan.id)
    return done


def _save_partial(ctx: StageContext, done: dict[str, dict], plans: dict[str, PagePlan]) -> None:
    items = {pid: {**rec, "input_hash": plans[pid].input_hash} for pid, rec in done.items()}
    artifacts.write_json(ctx.dir / PARTIAL, {**_partial_header(ctx), "items": items})


# ---------- 阶段入口 ----------

def run(ctx: StageContext) -> None:
    try:
        keyframes = ctx.read("keyframes.json")
    except FileNotFoundError as e:
        raise StageError("缺少 keyframes.json，请先完成关键帧提取") from e
    if not keyframes:
        ctx.write("slides.json", [])
        (ctx.dir / PARTIAL).unlink(missing_ok=True)
        ctx.progress(1.0, "没有检测到幻灯片")
        return

    language, plans = load_plans(ctx, keyframes)
    by_id = {p.id: p for p in plans}
    done = load_partial(ctx, plans)
    failed: dict[str, str] = {}
    size = max(1, ctx.settings.slides.batch_size)
    batches = [plans[i:i + size] for i in range(0, len(plans), size)]
    for n, batch in enumerate(batches):
        todo = [p for p in batch if p.id not in done]
        if not todo:
            continue
        ctx.progress(n / len(batches), f"读取幻灯片 {n * size + 1}–{n * size + len(batch)} / {len(plans)}"
                     + (f"（已失败 {len(failed)} 页）" if failed else ""))
        got, bad = read_batch(ctx, language, todo)
        done.update(got)
        failed.update(bad)
        _save_partial(ctx, done, by_id)

    slides = [{**(done[p.id] if p.id in done else failed_record(p)), "appearances": p.appearances} for p in plans]
    ctx.write("slides.json", slides)
    (ctx.dir / PARTIAL).unlink(missing_ok=True)
    bad_ids = [s["id"] for s in slides if s["status"] != "ok"]
    if bad_ids:
        shown = "、".join(bad_ids[:10]) + ("…" if len(bad_ids) > 10 else "")
        ctx.progress(1.0, f"幻灯片 {len(slides)} 页读取完成，其中 {len(bad_ids)} 页失败（{shown}），"
                          "后续步骤将改用讲解原文")
    else:
        ctx.progress(1.0, f"幻灯片 {len(slides)} 页读取完成")
