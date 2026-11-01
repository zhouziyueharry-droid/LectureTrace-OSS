"""阶段 outline（交接 0001 §7）：evidence.json → outline.json。只发文字。

1. 由 evidence.json（§6）构造按时间排序的「短索引」（`build_short_index`，§7.1）。同一个函数也用于
   发送前费用估算（send_plan，`placeholder_summaries=True`）和问答上下文（qa，§10.1），保证三处写法一致。
2. 结构化输出（`llm.complete_structured` + `schemas.OutlineResponse`，§8）生成章节、课程事务和无页 T 的分类。
3. 本地校验（`validate`）：证据 ID 必须在 `id_index` 中；节点 start 取所引证据最早的 jump；时间顺序检查。
   严重问题（无效 ID、顺序明显错乱）通过 `complete_structured(check=...)` 带反馈重试一次；
   重试后仍有问题时保留结果，问题写入 warnings。外层不再另做重试。

产物 outline.json（data）：
    {"chapters": [{"title", "summary", "evidence": [ids], "start", "children": [{..., "children": []}]}],
     "admin": {"title", "items": [{"title", "summary", "evidence", "start"}]},
     "speech_categories": {"T0040": "media"},   # 只含无页 T；模型输出的数组转成字典存（与 slides.json §5.4 一致）
     "warnings": [...]}
"""
from __future__ import annotations

import logging
from typing import Any

from ..llm import client as llm, schemas, settings as llm_settings
from .base import StageContext, StageError
from .evidence import fmt_time, id_index

log = logging.getLogger(__name__)

ORDER_TOLERANCE_S = 60.0   # 相邻节点回退超过此秒数视为一次倒序
ORDER_MAX_RATIO = 0.2      # 倒序占比超过此值视为「明显错乱」
MAX_OUTPUT_TOKENS = 32000  # 含推理 token

# ---------- 占位长度（send_plan 估算用，读图尚未完成时代替读图结果） ----------
# 来源：v2 付费实验（2026-09-27，S086–S095，10 页 / 10 个出现，effort=low），
# docs/proposals/0001-experiments/v2_exp/result_v2.json，用 tokens.text_tokens（o200k_base）统计：
# - spoken_summary 每个出现平均 Luna 91.7 / Sol 93.5 token（约 429 / 410 字符）→ 93
# - visual_content 每页平均 Luna 236.1 / Sol 279.6 token（约 658 / 756 字符，含 LaTeX）→ 258
# - title 每页平均约 26 / 23 字符 → 7 token
PLACEHOLDER_SUMMARY_TOKENS = 93
PLACEHOLDER_VISUAL_TOKENS = 258
PLACEHOLDER_TITLE_TOKENS = 7
PLACEHOLDER_CATEGORY = "lecture"
_FILLER = " word"          # o200k_base 下每个重复单元恰好 1 token

# 短索引的格式说明：生成大纲与问答的系统提示都可以引用，保证模型读到的说明与实际格式一致
SHORT_INDEX_LEGEND = """短索引按时间排序，每条记录是一次「出现」（一页幻灯片的一次连续显示）或一段无幻灯片的时段：
- 出现：[S012.1] 显示时间 | S012 标题 | 画面内容 | 这次老师讲了什么（摘要）。
  同一页的画面内容只在首次出现时给出，之后写「同 S012」。
  「（可能与 S011 是同一页的不同批注状态）」表示这页与时间上紧邻的前一页标题相同、画面高度相似。
- 无幻灯片时段：[N001] 讲解时间 | 无幻灯片。
- 记录下面缩进列出所含讲解片段 T 的 ID 与类别：lecture 讲课 / admin 课程事务 / interaction 课堂互动 /
  media 播放影音 / offtopic 闲聊 / mixed 混合 / uncertain 无法判断 / ? 未分类。
- 读图失败或未读图的页、无幻灯片时段，以及讲解过长被截断的出现，没有被摘要概括的 T 直接附原文：
  [T0040 20:31–21:29 类别] 原文。"""

SYSTEM_TEMPLATE = """你在为一节课程视频生成层级大纲。输入是整节课的「短索引」。

{legend}

要求：
1. 以幻灯片为骨架组织章节，最多两层（chapters，每章可有 children），按时间顺序覆盖整节课。
   附了原文的 T 同样要读：讲解中出现而幻灯片没有的主题也要单独成节。
2. 章节收讲课内容：lecture、mixed、uncertain（一律保留，避免漏掉讲课内容），以及有教学内容的
   interaction / media（例如课堂提问、投票讲解、教学片段）。只排除与课程无关的内容：纯等待、无关影片、闲聊。
3. admin 类内容（作业截止、考试安排、考勤、课程安排等）放进 admin.items；没有就给空数组。
4. 标注为「可能是同一页的不同批注状态」、且时间上相邻、属于同一段连续主题的几次出现，可以合并成一个节点，
   evidence 同时列出各次出现的 ID，不丢任何一次出现。相隔很远的回访（同一页后来又被翻回）不要并进前面的节点，
   按它自己的时间单独出现。
5. 每个节点的 evidence 必须引用支撑它的 ID，只能用输入中出现过的 ID：页（S012）、出现（S012.1）、
   无幻灯片时段（N001）或讲解片段（T0031）。
6. {language_rule}
7. speech_categories 只给无幻灯片时段（[N…]）里的 T 分类，每个这样的 T 恰好一条；没有则给空数组。"""

ADMIN_TITLES = {"zh-CN": "课程事务", "en": "Course logistics"}


# ---------- 短索引（§7.1） ----------

def _filler(n_tokens: int) -> str:
    return (_FILLER * n_tokens).strip()


def _one_line(text: str) -> str:
    return (text or "").strip().replace("\r\n", "\n").replace("\n", " ⏎ ")


def _span(a, b) -> str:
    if a is None:
        return "??:??"
    return f"{fmt_time(float(a))}–{fmt_time(float(b if b is not None else a))}"


def _section_time(sec: dict) -> tuple[Any, Any]:
    if sec.get("page") is not None and sec.get("display_start") is not None:
        return sec["display_start"], sec.get("display_end")
    return sec.get("speech_start"), sec.get("speech_end")


def _sort_key(sec: dict) -> tuple[float, str]:
    j = sec.get("jump")
    if j is None:
        j = _section_time(sec)[0]
    return (float(j) if j is not None else float("inf"), sec["id"])


def _same_title(a: str | None, b: str | None) -> bool:
    a, b = (a or "").strip().casefold(), (b or "").strip().casefold()
    return bool(a) and a == b


def build_short_index(evidence: dict, *, placeholder_summaries: bool = False, raw_text: bool = True,
                      extra_categories: dict[str, str] | None = None) -> str:
    """把 evidence.json 的 data（§6：pages / sections / speech）写成短索引文本（§7.1），按时间排序。

    返回不含前后说明的索引正文（每条记录一行，T 列表缩进在下面）；格式说明见 `SHORT_INDEX_LEGEND`。

    参数：
    - placeholder_summaries：发送前估算用。读图还没做，页标题、画面内容、出现摘要、T 类别都换成固定长度的占位文字
      （长度见模块常量 PLACEHOLDER_*，来源见注释），页一律按读图成功处理。结构、时间、T 原文与真实生成完全相同。
      截断情况取 section 的 `truncated_t_ids`（估算方按 §5.1 规则预先算好填入）。
    - raw_text：没有被摘要概括的 T（读图失败/跳过的页、无页段、截断出现中被截掉的 T）是否附原文。
      大纲用 True（§7.1）；问答（§10.1）可传 False，只列出 ID 与类别并注明「可展开原文」。
    - extra_categories：额外的 T 类别（例如 outline.json 的 speech_categories，无页 T 的分类），
      优先级低于 evidence 里已有的 category。问答复用时传入，大纲生成时不传。

    规则（§7.1）：
    - 同一页的 visual_content 只在该页首次出现时给出，之后写「同 S012」。
    - 页的 similar_prev 不为空且两页标题相同时，在首次出现的标题后写「（可能与 S011 是同一页的不同批注状态）」；
      占位模式下标题未知，只要 similar_prev 不为空就写（估算宁多勿少）。
    - 所有类别都保留，不在输入端过滤。
    """
    pages = {p["id"]: p for p in evidence.get("pages") or []}
    speech = {t["id"]: t for t in evidence.get("speech") or []}
    extra = extra_categories or {}
    sections = sorted(evidence.get("sections") or [], key=_sort_key)
    seen_pages: set[str] = set()
    lines: list[str] = []

    def cat(tid: str) -> str:
        if placeholder_summaries:
            return PLACEHOLDER_CATEGORY
        c = (speech.get(tid) or {}).get("category") or extra.get(tid)
        return c or "?"

    for sec in sections:
        start, end = _section_time(sec)
        t_ids = [t for t in sec.get("t_ids") or [] if t in speech]
        pid = sec.get("page")
        if pid is None:
            head = f"[{sec['id']}] {_span(start, end)} | 无幻灯片"
            if "media_candidate" in (sec.get("local_tags") or []):
                head += "（本机检测：出现非课程语言文字，可能在播放影片）"
            lines.append(head)
            summarized, unsummarized, note = [], t_ids, ""
        else:
            page = pages.get(pid) or {"id": pid, "status": "skipped"}
            status = "ok" if placeholder_summaries else (page.get("status") or "ok")
            first = pid not in seen_pages
            seen_pages.add(pid)
            if status == "ok":
                title = _filler(PLACEHOLDER_TITLE_TOKENS) if placeholder_summaries else (page.get("title") or "").strip()
                if first:
                    vis = (_filler(PLACEHOLDER_VISUAL_TOKENS) if placeholder_summaries
                           else _one_line(page.get("visual_content") or "")) or "（无）"
                else:
                    vis = f"同 {pid}"
                summ = (_filler(PLACEHOLDER_SUMMARY_TOKENS) if placeholder_summaries
                        else _one_line(sec.get("spoken_summary") or ""))
                if not summ:
                    summ = "（无讲解）" if not t_ids else "（无摘要）"
                hint = ""
                sp = page.get("similar_prev")
                if first and sp and sp.get("id"):
                    prev = pages.get(sp["id"]) or {}
                    if placeholder_summaries or _same_title(page.get("title"), prev.get("title")):
                        hint = f"（可能与 {sp['id']} 是同一页的不同批注状态）"
                lines.append(f"[{sec['id']}] {_span(start, end)} | {pid} {title}{hint} | {vis} | {summ}")
                cut = set(sec.get("truncated_t_ids") or [])
                summarized = [t for t in t_ids if t not in cut]
                unsummarized = [t for t in t_ids if t in cut]
                note = "讲解过长，摘要只概括了前一部分；以下 T 未被概括，附原文"
            else:
                label = "读图失败" if status == "failed" else "未读图"
                lines.append(f"[{sec['id']}] {_span(start, end)} | {pid}（{label}，以讲解原文代替）")
                summarized, unsummarized, note = [], t_ids, ""
        if summarized:
            lines.append("  " + ", ".join(f"{t} {cat(t)}" for t in summarized))
        if unsummarized:
            if raw_text:
                if note:
                    lines.append(f"  （{note}）")
                for t in unsummarized:
                    tt = speech[t]
                    c = cat(t)
                    tag = f" {c}" if c != "?" else ""
                    lines.append(f"  [{t} {_span(tt.get('start'), tt.get('end'))}{tag}] {_one_line(tt.get('text') or '')}")
            else:
                kind = "摘要不完整" if summarized else "未概括"
                lines.append("  " + ", ".join(f"{t} {cat(t)}" for t in unsummarized) + f"（{kind}，可展开原文）")
    return "\n".join(lines)


def output_language() -> tuple[str, str]:
    """(语言代码, 显示名)，取自设置（§10.5）。"""
    code = llm_settings.get()["output_language"]
    return code, llm_settings.OUTPUT_LANGUAGES.get(code, code)


def build_system(language_name: str) -> str:
    rule = f"用 {language_name} 输出标题和摘要，专有名词保留原文。"
    return SYSTEM_TEMPLATE.format(legend=SHORT_INDEX_LEGEND, language_rule=rule)


def build_request(evidence: dict, *, placeholder_summaries: bool = False) -> tuple[str, list[dict]]:
    """(system, messages)：生成大纲实际发送的内容。send_plan 用同一函数估算输入 token。"""
    _, name = output_language()
    index = build_short_index(evidence, placeholder_summaries=placeholder_summaries)
    user = "以下是本节课的短索引：\n\n" + index + "\n\n请生成大纲 JSON。"
    return build_system(name), [{"role": "user", "content": user}]


# ---------- 校验用的 ID ----------

def no_page_t_ids(evidence: dict) -> list[str]:
    """无页 section 里的 T（需要由大纲给出分类，§7.2）。"""
    return [t for s in evidence.get("sections") or [] if s.get("page") is None for t in s.get("t_ids") or []]


# ---------- 校验（§7.2） ----------

def _norm_ids(ev) -> list[str]:
    if isinstance(ev, str):
        ev = [ev]
    return [str(x).strip().strip("[]").strip() for x in ev or []]


def _node(n: Any, depth: int) -> dict:
    d = n.model_dump() if hasattr(n, "model_dump") else dict(n)
    node = {"title": str(d.get("title") or ""), "summary": str(d.get("summary") or ""),
            "evidence": _norm_ids(d.get("evidence")), "start": 0.0}
    if depth == 0:
        node["children"] = [_node(c, 1) for c in d.get("children") or []]
    else:
        node["children"] = []
    return node


def validate(resp: schemas.OutlineResponse | dict, index: dict[str, dict], *,
             no_page_t: list[str] | None = None, language: str = "zh-CN") -> tuple[dict, list[str], list[str]]:
    """清洗、去掉无效 ID、填 start、检查时间顺序与无页 T 分类。

    返回 (outline, warnings, feedback)：
    - outline：写入 outline.json 的结构（不含 warnings）；
    - warnings：全部问题（中文，写入产物）；
    - feedback：需要带反馈重试的严重问题（英文，发给模型）：无效证据 ID、章节时间顺序明显错乱。
    """
    if isinstance(resp, dict):
        resp = schemas.OutlineResponse.model_validate(resp)
    jumps = {k: float(v["jump"]) for k, v in index.items()}
    warnings: list[str] = []
    feedback: list[str] = []
    invalid: list[str] = []

    def fill(node: dict, fallback: float, *, admin: bool = False) -> float:
        bad = [i for i in node["evidence"] if i not in jumps]
        invalid.extend(bad)
        node["evidence"] = list(dict.fromkeys(i for i in node["evidence"] if i in jumps))
        child_starts = [fill(c, fallback) for c in node["children"]]
        own = [jumps[i] for i in node["evidence"]]
        if own:
            node["start"] = min(own)
        elif child_starts:
            node["start"] = min(child_starts)
        else:
            node["start"] = fallback
            warnings.append(f"{'课程事务' if admin else '节点'}「{node['title']}」没有有效证据")
        return node["start"]

    chapters = [_node(c, 0) for c in resp.chapters]
    prev = 0.0
    for ch in chapters:
        prev = fill(ch, prev)
    items = [_node(i, 1) for i in resp.admin.items]
    for it in items:
        fill(it, 0.0, admin=True)
        del it["children"]
    items.sort(key=lambda n: n["start"])

    if invalid:
        uniq = list(dict.fromkeys(invalid))
        warnings.append("存在无效证据 ID：" + "、".join(uniq))
        feedback.append("These evidence IDs do not exist in the input: " + ", ".join(uniq)
                        + ". Cite only IDs that appear in the short index.")
    for label, seq, serious in [("章节", chapters, True)] + [
            (f"「{c['title']}」的小节", c["children"], False) for c in chapters]:
        if len(seq) < 2:
            continue
        drops = sum(1 for a, b in zip(seq, seq[1:]) if b["start"] < a["start"] - ORDER_TOLERANCE_S)
        if drops and drops / (len(seq) - 1) > ORDER_MAX_RATIO:
            warnings.append(f"{label}时间顺序明显错乱（{drops} 处倒序）")
            if serious:
                feedback.append(f"Chapters are not in chronological order ({drops} backward jumps). "
                                "Order chapters by the time of their evidence.")
            else:
                feedback.append(f"Children of chapter '{label}' are not in chronological order.")

    # 无页 T 的分类：只收属于无页段的 T，每个一次；缺的只警告不补（未知就是未知）
    allowed = list(dict.fromkeys(no_page_t or []))
    allowed_set = set(allowed)
    cats: dict[str, str] = {}
    extra = []
    for sc in resp.speech_categories:
        if sc.id in allowed_set:
            cats.setdefault(sc.id, sc.category)
        else:
            extra.append(sc.id)
    if extra:
        warnings.append("speech_categories 含非无页讲解片段，已忽略：" + "、".join(dict.fromkeys(extra)))
    missing = [t for t in allowed if t not in cats]
    if missing:
        warnings.append(f"{len(missing)} 个无页讲解片段没有分类：" + "、".join(missing[:20])
                        + ("…" if len(missing) > 20 else ""))

    admin_title = (resp.admin.title or "").strip() or ADMIN_TITLES.get(language, ADMIN_TITLES["zh-CN"])
    outline = {"chapters": chapters, "admin": {"title": admin_title, "items": items},
               "speech_categories": {t: cats[t] for t in allowed if t in cats}}
    return outline, warnings, feedback


# ---------- 生成 ----------

def _empty(warning: str, language: str) -> dict:
    return {"chapters": [], "admin": {"title": ADMIN_TITLES.get(language, ADMIN_TITLES["zh-CN"]), "items": []},
            "speech_categories": {}, "warnings": [warning]}


def generate(evidence: dict, *, video_id: str | None) -> dict:
    lang, _ = output_language()
    index = id_index(evidence)
    npt = no_page_t_ids(evidence)
    system, messages = build_request(evidence)

    def check(resp: schemas.OutlineResponse) -> list[str]:
        return validate(resp, index, no_page_t=npt, language=lang)[2]

    try:
        resp = llm.complete_structured(messages=messages, response_model=schemas.OutlineResponse, system=system,
                                       max_tokens=MAX_OUTPUT_TOKENS, video_id=video_id, stage="outline",
                                       effort=llm_settings.effort_for("outline"), retries=1, check=check)
    except llm.LLMInvalidOutputError as e:
        # 重试后仍有问题：格式合格（只是 check 不通过）就保留结果并写 warnings，格式不合格才算失败
        try:
            resp = schemas.OutlineResponse.model_validate(llm.parse_json(e.raw_text))
        except Exception:  # noqa: BLE001
            raise StageError(f"生成大纲失败：模型输出不符合格式要求（{'；'.join(e.problems[:3])}）") from e
        log.warning("大纲重试后仍有问题，保留结果：%s", "; ".join(e.problems[:3]))
    except (llm.LLMRefusalError, llm.LLMTruncatedError) as e:
        raise StageError(f"生成大纲失败：{e}") from e

    outline, warnings, _ = validate(resp, index, no_page_t=npt, language=lang)
    pages = evidence.get("pages") or []
    if pages and all((p.get("status") or "ok") == "skipped" for p in pages):
        warnings.insert(0, "幻灯片未读图，大纲只依据讲解原文生成")
    failed = [p["id"] for p in pages if p.get("status") == "failed"]
    if failed:
        warnings.insert(0, f"{len(failed)} 页读图失败，以讲解原文代替：" + "、".join(failed[:20]))
    outline["warnings"] = warnings
    return outline


def run(ctx: StageContext) -> None:
    from ..qa import outline_edits
    try:
        evidence = ctx.read("evidence.json")
    except FileNotFoundError as e:
        raise StageError("缺少 evidence.json，请先完成证据整理") from e
    if isinstance(evidence, list):
        raise StageError("evidence.json 是旧格式，请重新运行证据整理")
    lang, _ = output_language()
    evidence = evidence or {}
    with outline_edits.lock(ctx.video_id):
        try:
            ctx.outline_base_revision = outline_edits.ensure(ctx.video_id)["revision"]
        except FileNotFoundError:
            ctx.outline_base_revision = None
        ctx.outline_evidence_version = outline_edits.fingerprint(evidence)
    if not evidence.get("sections"):
        try:
            outline_edits.persist_generated(ctx, _empty("没有可用的字幕或幻灯片，无法生成大纲", lang))
        except outline_edits.OutlineEditError as error:
            raise StageError(str(error)) from error
        ctx.progress(1.0, "没有可用证据")
        return
    ctx.progress(0.1, "生成大纲")
    outline = generate(evidence, video_id=ctx.video_id)
    try:
        outline_edits.persist_generated(ctx, outline)
    except outline_edits.OutlineEditError as error:
        raise StageError(str(error)) from error
    msg = f"大纲共 {len(outline['chapters'])} 章"
    if outline["admin"]["items"]:
        msg += f"，课程事务 {len(outline['admin']['items'])} 条"
    if outline["warnings"]:
        msg += f"，{len(outline['warnings'])} 条警告"
    ctx.progress(1.0, msg)
