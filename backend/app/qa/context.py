"""问答上下文（交接 0001 §10.1–10.2）。

缓存前缀 = 系统规则 + 大纲 + 短索引（`outline.build_short_index(raw_text=False)` 与 `SHORT_INDEX_LEGEND`，与大纲同一写法）。
内容必须对同一视频逐字节稳定（不含当前时间等易变内容），否则提示缓存失效；两步请求（定位、回答）共用同一前缀。

本模块还负责：
- 定位请求的用户消息（`locate_prompt`）与宽松解析（`parse_locate`，失败按「无需展开」）；
- 按定位结果展开讲解原文与截图（`expand`），上限取 `QaParams`；
- 回答请求的用户消息（`answer_content`：问题 + 【展开的证据】）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import artifacts
from ..config import QaParams, settings
from ..llm import client as llm, settings as llm_settings
from ..pipeline.evidence import ID_RE, fmt_time, id_index
from ..pipeline.outline import SHORT_INDEX_LEGEND, build_short_index

log = logging.getLogger(__name__)

NOT_FOUND = {"zh-CN": "视频中未找到相关内容", "en": "No relevant content was found in this video."}

RULES_TEMPLATE = """你是一节课程视频的学习助手。下面给出这节课的大纲和「短索引」：短索引按时间列出每一次幻灯片出现（画面转录与讲解摘要）
和无幻灯片时段，以及其中每个讲解片段 T 的 ID 与类别。讲解原文不在这里，需要时按 ID 展开。

每一轮问答分两步：
1. 定位：用户消息以「【定位】」开头时，只判断回答需要展开哪些证据，按用户消息的要求只输出 JSON，不要作答。
2. 回答：用户消息给出问题，并在「【展开的证据】」中附上展开的讲解原文和幻灯片截图（可能为空）。据此作答。

回答规则：
1. 只依据大纲、短索引和展开的证据回答，不要用证据之外的知识补充课程内容，不要编造。
2. 证据中找不到依据时，直接回答「{not_found}」，可以简要说明视频中最接近的内容（同样附证据）。
3. 用{language}回答，专有名词保留原文；使用 Markdown，公式用 LaTeX。
4. 每个论断后面都要附上支撑它的证据 ID，写成 [S012.1]、[S012]、[N001] 或 [T0142]；一个方括号里只放一个 ID，
   多个就写 [S012.1][T0142]。只能引用短索引中出现过的 ID，或本轮展开的 ID。
   讲解内容优先引用讲解片段 T 或出现 S012.1，画面内容引用页 S012 或出现。
5. 回答简洁、准确，适合复习。"""

QA_INDEX_NOTE = """问答中的短索引不附讲解原文（与上面说明的区别）：读图失败或未读图的页、无幻灯片时段里的 T 只列出 ID 与类别，
标注「（未概括，可展开原文）」；讲解过长被截断的出现，没有被摘要概括的 T 标注「（摘要不完整，可展开原文）」。
问题涉及这些内容时，要在定位步骤展开它们。"""

LOCATE_TEMPLATE = """【定位】学生的问题：
{question}

先不要回答。判断回答这个问题需要展开哪些证据：
- expand：需要讲解原文时，列出讲解片段（T0031）、出现（S012.1）、无幻灯片时段（N001）或页（S012）的 ID。
  出现和时段展开为其中全部讲解片段，页展开为它各次出现的讲解片段。最多 {max_ids} 个，按重要程度排序。
- images：需要看幻灯片画面细节（公式、图表、手写批注）时，列出页（S012）或出现（S012.1，取那一次的截图）的 ID，最多 {max_images} 个。
- 短索引已足够回答，或问题与课程无关时，给空数组。
只输出一个 JSON 对象，不要任何其他文字，例如：{{"expand": ["S012.1", "T0031"], "images": ["S012"]}}"""


class ContextError(Exception):
    """问答上下文不可用（例如证据尚未生成）。"""


@dataclass
class QaContext:
    video_id: str
    system: list[dict]                  # system 块；最后一块由 llm 客户端加缓存断点
    evidence: dict                      # evidence.json 的 data（pages / sections / speech）
    index: dict[str, dict]              # id_index(evidence)
    version: str | None                 # evidence.json 的 created_at，写进 chat_messages.evidence_version
    prefix_ids: frozenset[str]          # 系统前缀（大纲 + 短索引）中出现、且存在于 index 的 ID
    language: str                       # 输出语言代码（zh-CN / en）

    @property
    def not_found(self) -> str:
        return NOT_FOUND.get(self.language, NOT_FOUND["zh-CN"])


@dataclass
class Expansion:
    """定位结果展开后的内容。"""
    t_ids: list[str] = field(default_factory=list)      # 实际附上原文的 T（按时间排序）
    ids: list[str] = field(default_factory=list)        # 本轮展开的 ID：定位给出的合法 ID + 展开出的 T + 截图 ID
    truncated: list[str] = field(default_factory=list)  # 因上限没有完整展开的定位 ID
    images: list[tuple[str, str]] = field(default_factory=list)  # (ID, base64 JPEG)
    text: str = ""                                       # 【展开的证据】的文字部分


# ---------------------------------------------------------------- 读取产物

def _video_path(video_id: str, name: str) -> Path:
    return artifacts.video_dir(video_id) / name


def load_evidence(video_id: str) -> tuple[dict, str | None]:
    """(evidence data, 版本 = created_at)。没有证据或是 v1 旧格式时抛 ContextError。"""
    path = _video_path(video_id, "evidence.json")
    try:
        obj = artifacts.read_json(path)
    except FileNotFoundError as e:
        raise ContextError("这个视频还没有整理证据，暂时无法问答") from e
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        raise ContextError("这个视频的证据是旧格式，请重新运行「整理证据」后再问答")
    return data, obj.get("created_at")


def current_version(video_id: str) -> str | None:
    """当前 evidence.json 的版本；不存在或无法读取时为 None。"""
    try:
        obj = artifacts.read_json(_video_path(video_id, "evidence.json"))
    except (FileNotFoundError, ValueError):
        return None
    return obj.get("created_at") if isinstance(obj, dict) else None


def load_outline(video_id: str) -> dict | None:
    try:
        data = artifacts.read_artifact(video_id, "outline.json")
    except (FileNotFoundError, ValueError, KeyError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 系统前缀

def _ids(ev: Any) -> str:
    if isinstance(ev, str):
        ev = [ev]
    return "".join(f"[{e}]" for e in ev or [])


def _when(node: dict) -> str:
    s = node.get("start")
    return f"（{fmt_time(float(s))}）" if isinstance(s, (int, float)) else ""


def outline_text(outline: dict | None) -> str:
    """大纲的文字形式：章节（最多两层）+ 课程事务。没有大纲时为「（暂无大纲）」。"""
    if not outline or not (outline.get("chapters") or (outline.get("admin") or {}).get("items")):
        return "（暂无大纲）"
    lines: list[str] = []
    for i, ch in enumerate(outline.get("chapters") or [], 1):
        lines.append(f"{i}. {ch.get('title', '')}{_when(ch)} {_ids(ch.get('evidence'))}".rstrip())
        if ch.get("summary"):
            lines.append(f"   {ch['summary']}")
        for j, c in enumerate(ch.get("children") or [], 1):
            lines.append(f"   {i}.{j} {c.get('title', '')}{_when(c)} {_ids(c.get('evidence'))}".rstrip())
            if c.get("summary"):
                lines.append(f"      {c['summary']}")
    admin = outline.get("admin") or {}
    if admin.get("items"):
        lines.append(f"{admin.get('title') or '课程事务'}：")
        for it in admin["items"]:
            lines.append(f"- {it.get('title', '')}{_when(it)} {_ids(it.get('evidence'))}".rstrip())
            if it.get("summary"):
                lines.append(f"  {it['summary']}")
    return "\n".join(lines)


def _extra_categories(outline: dict | None) -> dict[str, str]:
    """outline.json 的 speech_categories（无页 T 的分类）；兼容数组格式。"""
    raw = (outline or {}).get("speech_categories") or {}
    if isinstance(raw, list):
        raw = {c.get("id"): c.get("category") for c in raw if isinstance(c, dict)}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}


def build_rules(language: str) -> str:
    name = llm_settings.OUTPUT_LANGUAGES.get(language, language)
    return RULES_TEMPLATE.format(not_found=NOT_FOUND.get(language, NOT_FOUND["zh-CN"]), language=name)


def build_context(video_id: str) -> QaContext:
    """读取 evidence.json / outline.json，拼出缓存前缀。同一份产物、同一输出语言下逐字节相同。"""
    evidence, version = load_evidence(video_id)
    outline = load_outline(video_id)
    language = llm_settings.get()["output_language"]
    index = id_index(evidence)
    short = build_short_index(evidence, raw_text=False, extra_categories=_extra_categories(outline))
    body = "\n\n".join([
        "# 短索引格式说明\n" + SHORT_INDEX_LEGEND + "\n" + QA_INDEX_NOTE,
        "# 大纲\n" + outline_text(outline),
        "# 短索引\n" + (short or "（没有可用的证据）"),
    ])
    system = [{"type": "text", "text": build_rules(language)}, {"type": "text", "text": body}]
    prefix_ids = frozenset(i for i in ID_RE.findall(body) if i in index)
    return QaContext(video_id=video_id, system=system, evidence=evidence, index=index, version=version,
                     prefix_ids=prefix_ids, language=language)


# ---------------------------------------------------------------- 第 1 步：定位

def locate_prompt(question: str, params: QaParams | None = None) -> str:
    p = params or settings.qa
    return LOCATE_TEMPLATE.format(question=question, max_ids=p.max_expand_ids, max_images=p.max_expand_images)


def _id_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, str):
            s = x.strip().strip("[]【】").strip()
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


def parse_locate(text: str, index: dict[str, dict], params: QaParams | None = None) -> tuple[list[str], list[str]]:
    """宽松解析定位回复 → (expand ID, 图片 ID)。不合法的 ID 丢弃并记日志；解析失败按「无需展开」。"""
    p = params or settings.qa
    try:
        data = llm.parse_json(text or "")
    except ValueError:
        log.warning("定位回复不是有效的 JSON，按无需展开处理：%r", (text or "")[:200])
        return [], []
    if isinstance(data, list):   # 只给了数组：当作 expand
        data = {"expand": data}
    if not isinstance(data, dict):
        log.warning("定位回复格式不对，按无需展开处理：%r", (text or "")[:200])
        return [], []
    exp_raw = _id_list(data.get("expand"))
    img_raw = _id_list(data.get("images"))
    expand = [i for i in exp_raw if i in index]
    images = [i for i in img_raw if (index.get(i) or {}).get("kind") in ("page", "section")]
    bad = [i for i in exp_raw if i not in index] + [i for i in img_raw if i not in images]
    if bad:
        log.warning("定位结果中的无效 ID 已丢弃：%s", ", ".join(bad))
    if len(expand) > p.max_expand_ids:
        log.info("定位给出 %d 个 ID，只取前 %d 个", len(expand), p.max_expand_ids)
    if len(images) > p.max_expand_images:
        log.info("定位给出 %d 张图，只取前 %d 张", len(images), p.max_expand_images)
    return expand[:p.max_expand_ids], images[:p.max_expand_images]


# ---------------------------------------------------------------- 展开

def _t_of(ev_id: str, ctx: QaContext) -> list[str]:
    kind = (ctx.index.get(ev_id) or {}).get("kind")
    if kind == "speech":
        return [ev_id]
    sections = {s["id"]: s for s in ctx.evidence.get("sections") or []}
    if kind == "section":
        return list(sections.get(ev_id, {}).get("t_ids") or [])
    if kind == "page":
        page = next((p for p in ctx.evidence.get("pages") or [] if p["id"] == ev_id), {})
        return [t for sid in page.get("sections") or [] for t in sections.get(sid, {}).get("t_ids") or []]
    return []


def _image_path(ev_id: str, ctx: QaContext) -> str | None:
    """页取页截图；出现取当次截图（§3.7.1），没有就退回所属页的截图；无页段没有截图。"""
    kind = (ctx.index.get(ev_id) or {}).get("kind")
    pages = {p["id"]: p for p in ctx.evidence.get("pages") or []}
    if kind == "page":
        return (pages.get(ev_id) or {}).get("image")
    if kind == "section":
        sec = next((s for s in ctx.evidence.get("sections") or [] if s["id"] == ev_id), {})
        if sec.get("image"):
            return sec["image"]
        if sec.get("page"):
            return (pages.get(sec["page"]) or {}).get("image")
    return None


def _encode(path: Path) -> str:
    from ..pipeline.slides import encode_image  # 与读幻灯片同一缩放（SlideParams.max_edge）

    return encode_image(path, settings.slides.max_edge)


def expand(ctx: QaContext, expand_ids: list[str], image_ids: list[str], params: QaParams | None = None) -> Expansion:
    """按定位结果展开讲解原文和截图。

    讲解原文：按定位结果的顺序依次加入各 ID 的 T（去重），总数不超过 max_expand_t、总字符不超过 max_expand_chars；
    超出时停止，没有完整展开的 ID 记入 truncated，并在附加内容里注明「已截断」。展示时按时间排序。
    截图：页或出现，文件不存在就跳过。
    """
    p = params or settings.qa
    speech = {t["id"]: t for t in ctx.evidence.get("speech") or []}
    chosen: list[str] = []
    chars = 0
    full = True
    truncated: list[str] = []
    for ev_id in expand_ids:
        ts = [t for t in _t_of(ev_id, ctx) if t in speech]
        complete = True
        for t in ts:
            if t in chosen:
                continue
            n = len(speech[t].get("text") or "")
            if not full or len(chosen) >= p.max_expand_t or (chosen and chars + n > p.max_expand_chars):
                full = False
                complete = False
                continue
            chosen.append(t)
            chars += n
        if not complete:
            truncated.append(ev_id)

    exp = Expansion(truncated=truncated)
    exp.t_ids = sorted(chosen, key=lambda t: (float(speech[t].get("start") or 0), t))

    vdir = artifacts.video_dir(ctx.video_id)
    for ev_id in image_ids:
        rel = _image_path(ev_id, ctx)
        path = vdir / rel if rel else None
        if not path or not path.is_file():
            log.warning("截图不存在，跳过：%s（%s）", ev_id, rel)
            continue
        try:
            exp.images.append((ev_id, _encode(path)))
        except Exception:  # noqa: BLE001  图片损坏不影响作答
            log.exception("截图读取失败，跳过：%s", path)

    exp.ids = list(dict.fromkeys([*expand_ids, *exp.t_ids, *(i for i, _ in exp.images)]))
    exp.text = _expansion_text(ctx, exp, speech, p)
    return exp


def _expansion_text(ctx: QaContext, exp: Expansion, speech: dict[str, dict], p: QaParams) -> str:
    lines = ["【展开的证据】"]
    if not exp.t_ids and not exp.images:
        lines.append("（无：短索引已足够，或没有需要展开的内容）")
        return "\n".join(lines)
    if exp.t_ids:
        lines.append("讲解原文（按时间排序）：")
        for t in exp.t_ids:
            tt = speech[t]
            span = f"{fmt_time(float(tt.get('start') or 0))}–{fmt_time(float(tt.get('end') or tt.get('start') or 0))}"
            text = (tt.get("text") or "").strip()
            if len(text) > p.max_expand_chars:   # 单段就超过上限（只会是第一段）
                text = text[:p.max_expand_chars] + "……（已截断）"
            lines.append(f"[{t} {span} | {tt.get('section') or '?'}] {text}")
    if exp.truncated:
        lines.append(f"（已截断：展开内容超过上限（{p.max_expand_t} 段或约 {p.max_expand_chars} 字符），"
                     f"以下 ID 未完整展开：{'、'.join(exp.truncated)}）")
    if exp.images:
        lines.append("幻灯片截图见下（每张图前标注 ID）。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 第 2 步：回答

def answer_content(question: str, exp: Expansion) -> str | list[dict]:
    """回答请求的最后一条用户消息：问题 + 【展开的证据】（T 原文与截图）。"""
    text = f"{question}\n\n{exp.text}"
    if not exp.images:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}]
    for ev_id, data in exp.images:
        blocks.append({"type": "text", "text": f"[截图 {ev_id}]"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    return blocks
