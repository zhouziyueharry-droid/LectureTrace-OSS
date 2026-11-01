"""阶段 cues：transcript.json → cues.json（短句字幕条目，ARCHITECTURE §3.2）。

规则：以词为单位，遇句末标点断句；超过长度或时长上限时，优先在逗号处、其次在较长停顿处再切；
词间长时间静音强制断开；过短条目在不重叠的前提下延长显示；相邻条目不重叠。
"""
from __future__ import annotations

from .base import StageContext, StageError

SENTENCE_END = (".", "?", "!", "。", "？", "！", "…")
CLAUSE_END = (",", ";", ":", "，", "；", "：", "、")
TRAILING = "\"')]}”’）】"  # 句末标点后可能跟的引号、括号
PAUSE_BREAK_S = 1.5   # 词间静音超过此值强制断开
PAUSE_SPLIT_S = 0.4   # 超限再切时，停顿超过此值才算"较长停顿"


def _ends_with(word: str, marks: tuple[str, ...]) -> bool:
    return word.rstrip().rstrip(TRAILING).endswith(marks)


def join_words(words: list[str]) -> str:
    """拼接 faster-whisper 的词（英文词自带前导空格）；缺空格的两个英文词之间补一个空格。"""
    text = ""
    for w in words:
        if text and w and not w[0].isspace() and text[-1].isascii() and text[-1].isalnum() \
                and w[0].isascii() and w[0].isalnum():
            text += " "
        text += w
    return " ".join(text.split())


def _text(ws: list[dict]) -> str:
    return join_words([w["word"] for w in ws])


def _split_point(cur: list[dict], min_s: float) -> int:
    """cur 已放不下下一个词时，返回切分位置 k（前 k 个词成一条）。

    优先最靠后的逗号，其次最长的停顿（>= PAUSE_SPLIT_S）；前半段至少 min_s 且不少于 1/3 的词，
    否则 k = len(cur)，即整条输出。
    """
    n = len(cur)
    lo = max(1, n // 3)
    ok = [k for k in range(lo, n) if cur[k - 1]["end"] - cur[0]["start"] >= min_s]
    commas = [k for k in ok if _ends_with(cur[k - 1]["word"], CLAUSE_END)]
    if commas:
        return commas[-1]
    gaps = [(cur[k]["start"] - cur[k - 1]["end"], k) for k in ok]
    gaps = [g for g in gaps if g[0] >= PAUSE_SPLIT_S]
    if gaps:
        return max(gaps)[1]
    return n


def split_cues(words: list[dict], max_chars: int = 84, max_s: float = 7.0,
               min_s: float = 1.0) -> list[dict]:
    """纯函数：words=[{"start","end","word"}]（按时间顺序）→ [{"id","start","end","text","zh"}]。"""
    words = [w for w in words if w.get("word", "").strip()]
    groups: list[list[dict]] = []
    cur: list[dict] = []

    for w in words:
        if cur:
            if w["start"] - cur[-1]["end"] >= PAUSE_BREAK_S:
                groups.append(cur)
                cur = []
            else:
                too_long = len(_text(cur + [w])) > max_chars or w["end"] - cur[0]["start"] > max_s
                while cur and too_long:
                    k = _split_point(cur, min_s)
                    groups.append(cur[:k])
                    cur = cur[k:]
                    too_long = bool(cur) and (len(_text(cur + [w])) > max_chars
                                              or w["end"] - cur[0]["start"] > max_s)
        cur.append(w)
        if _ends_with(w["word"], SENTENCE_END):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    cues = [{"start": float(g[0]["start"]), "end": float(g[-1]["end"]), "text": _text(g)} for g in groups]
    _fix_times(cues, min_s)
    return [{"id": f"C{i:04d}", "start": round(c["start"], 3), "end": round(c["end"], 3),
             "text": c["text"], "zh": None} for i, c in enumerate(cues, 1)]


def _fix_times(cues: list[dict], min_s: float) -> None:
    """保证 start < end、相邻不重叠；过短条目在不碰到下一条的前提下延长到 min_s。"""
    for i, c in enumerate(cues):
        if i > 0 and c["start"] < cues[i - 1]["end"]:
            c["start"] = cues[i - 1]["end"]  # 词时间戳偶有重叠，后一条往后挪
        if c["end"] <= c["start"]:
            c["end"] = c["start"] + 0.01
    for i, c in enumerate(cues):
        if c["end"] - c["start"] < min_s:
            limit = cues[i + 1]["start"] if i + 1 < len(cues) else float("inf")
            c["end"] = max(c["end"], min(c["start"] + min_s, limit))


def words_from_transcript(transcript: dict) -> list[dict]:
    """展开 segments 的词；某段缺词级时间戳时把整段当作一个词。"""
    out: list[dict] = []
    for seg in transcript.get("segments", []):
        ws = seg.get("words") or []
        if ws:
            out.extend(ws)
        elif seg.get("text", "").strip():
            out.append({"start": seg["start"], "end": seg["end"], "word": " " + seg["text"].strip()})
    return out


def run(ctx: StageContext) -> None:
    try:
        transcript = ctx.read("transcript.json")
    except FileNotFoundError:
        raise StageError("找不到 transcript.json，请先完成语音识别阶段") from None
    p = ctx.settings.cues
    cues = split_cues(words_from_transcript(transcript), p.max_chars, p.max_s, p.min_s)
    ctx.write("cues.json", cues)  # 无语音时写空列表，不阻断后续（大纲仍可由幻灯片生成）
    ctx.progress(1.0, f"生成 {len(cues)} 条字幕" if cues else "没有识别出语音内容，字幕为空")
