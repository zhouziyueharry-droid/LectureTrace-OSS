"""阶段 translate（ARCHITECTURE §3.4）：cues.json 补中文，生成 subs.source.vtt / subs.bilingual.vtt。

- 按生效设置选择 LLMTranslator、DeepLTranslator 或 GoogleTranslator。
- 每批 batch_size 条，附前后各 context_cues 条原文作上下文。
- 断点：已有 zh 的条目跳过；每批完成即写回 cues.json。
- cues.json 的 params：翻译中途的断点写入保留 cues 阶段的原值；全部完成时写入 translate 的参数
  （与 runner 的「最后写入者」参数检测一致）。若上次完成时的翻译参数与当前不同，则清空 zh 重译。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .. import artifacts, db
from ..keys import DEEPL_USD_PER_CHAR, GOOGLE_USD_PER_CHAR, is_free_key
from ..llm import client as llm
from .base import StageContext, StageError

log = logging.getLogger(__name__)


class Translator(ABC):
    """把一批字幕翻成中文。返回 {cue_id: 中文}，必须覆盖 batch 中全部 ID。"""

    name: str = ""

    @abstractmethod
    def translate(self, batch: list[dict], before: list[dict], after: list[dict], *,
                  video_id: str | None) -> dict[str, str]:
        ...


# ---------- LLM ----------

LLM_SYSTEM = """你是计算机科学 / 人工智能课程的字幕翻译。把英文课堂字幕逐条翻译成简体中文。
要求：
1. 输入 JSON 中 "cues" 是要翻译的条目；"context_before" / "context_after" 只是上下文，不要翻译、不要输出。
2. 输出一个 JSON 对象：键为 cues 中每条的 ID（原样照抄），值为该条的中文译文。条数必须与 cues 完全一致，不得合并、拆分、遗漏或新增。
3. CS/AI 专有名词、模型名、算法名、库名、代码标识符保留英文原文（如 Transformer、batch normalization、ReLU、gradient descent、PyTorch），数学符号与公式保持原样。
4. 字幕是口语，可能在句中断开：结合上下文译得通顺，但每条译文只对应该条原文的内容。
5. 只输出 JSON，不要任何解释或代码块标记。"""


class LLMTranslator(Translator):
    name = "LLM"

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def _call(self, batch, before, after, video_id) -> dict[str, str] | None:
        payload = {
            "context_before": [{"id": c["id"], "text": c["text"]} for c in before],
            "cues": [{"id": c["id"], "text": c["text"]} for c in batch],
            "context_after": [{"id": c["id"], "text": c["text"]} for c in after],
        }
        ids = [c["id"] for c in batch]
        content = json.dumps(payload, ensure_ascii=False)
        for attempt in range(self.max_attempts):
            try:
                out = llm.complete_json(
                    messages=[{"role": "user", "content": content}], system=LLM_SYSTEM,
                    max_tokens=8000, video_id=video_id, stage="translate", effort="low")
            except ValueError:
                log.warning("翻译结果不是 JSON，重试 %d", attempt + 1)
                continue
            got = _normalize(out)
            if got is not None and set(got) == set(ids) and all(got[i].strip() for i in ids):
                return {i: got[i].strip() for i in ids}
            log.warning("翻译条数/ID 不一致（期望 %d 条），重试 %d", len(ids), attempt + 1)
        return None

    def translate(self, batch, before, after, *, video_id=None):
        got = self._call(batch, before, after, video_id)
        if got is not None:
            return got
        if len(batch) == 1:
            raise StageError(f"字幕 {batch[0]['id']} 翻译失败：模型多次返回的格式不正确")
        # 多次不一致时对半拆分再试
        mid = len(batch) // 2
        left, right = batch[:mid], batch[mid:]
        res = self.translate(left, before, right[:2], video_id=video_id)
        res.update(self.translate(right, left[-2:], after, video_id=video_id))
        return res


def _normalize(out) -> dict[str, str] | None:
    """兼容 {"C0001": "..."} 与 [{"id","zh"}] 两种返回。"""
    if isinstance(out, dict):
        if "cues" in out and isinstance(out["cues"], (list, dict)):
            return _normalize(out["cues"])
        if all(isinstance(v, str) for v in out.values()):
            return dict(out)
        return None
    if isinstance(out, list):
        res = {}
        for item in out:
            if not isinstance(item, dict) or "id" not in item:
                return None
            zh = item.get("zh", item.get("text"))
            if not isinstance(zh, str):
                return None
            res[str(item["id"])] = zh
        return res
    return None


# ---------- DeepL ----------

# 简单术语表：这些词保持英文（DeepL 术语表源词 → 目标词）
DEFAULT_TERMS = [
    "Transformer", "attention", "self-attention", "embedding", "batch normalization",
    "layer normalization", "dropout", "ReLU", "softmax", "gradient descent", "backpropagation",
    "CNN", "RNN", "LSTM", "GAN", "encoder", "decoder", "token", "fine-tuning", "overfitting",
    "PyTorch", "TensorFlow", "GPU", "API",
]


class DeepLTranslator(Translator):
    name = "DeepL"

    def __init__(self, api_key: str, terms: list[str] | None = None, target_lang: str = "ZH-HANS"):
        if not api_key:
            raise StageError("未填写 DeepL API Key，请到设置页填写")
        import deepl
        self._deepl = deepl
        self.client = deepl.DeepLClient(api_key)
        self.free = is_free_key(api_key)
        self.terms = DEFAULT_TERMS if terms is None else terms
        self.target_lang = target_lang
        self._glossary = None
        self._glossary_tried = False

    def _get_glossary(self):
        """术语表按内容哈希缓存 ID，避免每次新建；失败则不用术语表。"""
        if self._glossary_tried or not self.terms:
            return self._glossary
        self._glossary_tried = True
        entries = {t: t for t in self.terms}
        digest = hashlib.sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:12]
        cached = db.get_setting("deepl_glossary")
        try:
            if cached and cached.get("digest") == digest:
                self._glossary = cached["id"]
            else:
                g = self.client.create_glossary(f"LectureTrace-{digest}", source_lang="EN",
                                                target_lang="ZH", entries=entries)
                self._glossary = g.glossary_id
                db.set_setting("deepl_glossary", {"id": g.glossary_id, "digest": digest})
        except Exception as e:  # noqa: BLE001
            log.warning("DeepL 术语表不可用，改为不使用术语表：%s", e)
            self._glossary = None
        return self._glossary

    def translate(self, batch, before, after, *, video_id=None):
        context = " ".join(c["text"] for c in before + batch + after)
        texts = [c["text"] for c in batch]
        glossary = self._get_glossary()
        kwargs = {"target_lang": self.target_lang, "context": context}
        if glossary:
            kwargs.update(source_lang="EN", glossary=glossary)
        try:
            results = self.client.translate_text(texts, **kwargs)
        except self._deepl.DeepLException as e:
            raise StageError(f"DeepL 翻译失败：{e}") from e
        chars = sum(len(t) for t in texts)
        db.record_usage(video_id=video_id, stage="translate", provider="DeepL", model=None,
                        cost_usd=0.0 if self.free else chars * DEEPL_USD_PER_CHAR)
        return {c["id"]: r.text for c, r in zip(batch, results)}


def google_request(api_key: str, payload: dict | None = None) -> dict:
    if not api_key:
        raise StageError("未填写 Google Translation API Key，请到设置页填写")
    url = "https://translation.googleapis.com/language/translate/v2"
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    request = Request(url if payload is not None else url + '/languages', data=body,
                      headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
    except HTTPError as error:
        message = {400: '请求无效，请检查 Key、输入和语言设置',
                   401: 'Key 无效或已失效',
                   403: '访问被拒绝，请检查 Cloud Translation API 是否启用、账单、Key 限制或配额',
                   429: '请求频率或配额已达上限，请稍后重试或检查 Google Cloud 配额'}.get(error.code, '服务暂时不可用，请稍后重试')
        raise StageError(f'Google 翻译失败（HTTP {error.code}）：{message}') from None
    except (URLError, TimeoutError, OSError):
        raise StageError('无法连接 Google 翻译，请检查网络后重试') from None
    except (ValueError, UnicodeError):
        raise StageError('Google 翻译返回了无效的 JSON') from None
    if not isinstance(result, dict):
        raise StageError('Google 翻译返回格式不正确')
    return result


class GoogleTranslator(Translator):
    name = 'Google Cloud Translation'

    def __init__(self, api_key: str):
        if not api_key:
            raise StageError('未填写 Google Translation API Key，请到设置页填写')
        self.api_key = api_key

    def translate(self, batch, before, after, *, video_id=None):
        if not batch:
            return {}
        # Basic v2 的协议上限：128 条、100 KB JSON 请求。
        # shortcut: 子请求共用批次 checkpoint；超大字幕重试可能重发，实际遇到时再细化保存粒度。
        if len(batch) > 128:
            result = {}
            for i in range(0, len(batch), 128):
                result.update(self.translate(batch[i:i + 128], [], [], video_id=video_id))
            return result
        payload = {'q': [cue['text'] for cue in batch], 'target': 'zh-CN', 'format': 'text', 'model': 'nmt'}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 100_000:
            if len(batch) == 1:
                raise StageError('单条字幕超过 Google 翻译请求大小限制，请先拆分字幕')
            mid = len(batch) // 2
            return {**self.translate(batch[:mid], [], [], video_id=video_id),
                    **self.translate(batch[mid:], [], [], video_id=video_id)}
        response = google_request(self.api_key, payload)
        chars = sum(len(cue['text']) for cue in batch)
        db.record_usage(video_id=video_id, stage='translate', provider='Google Translate', model='nmt',
                        cost_usd=chars * GOOGLE_USD_PER_CHAR)
        data = response.get('data')
        translations = data.get('translations') if isinstance(data, dict) else None
        if (not isinstance(translations, list) or len(translations) != len(batch)
                or any(not isinstance(item, dict) or not isinstance(item.get('translatedText'), str)
                       or not item['translatedText'].strip() for item in translations)):
            raise StageError('Google 翻译返回的条数或译文不正确，请重试')
        return {cue['id']: html.unescape(item['translatedText']) for cue, item in zip(batch, translations)}


def make_translator(settings) -> Translator:
    from .. import keys

    provider = settings.translation_provider()
    if provider == 'ollama':
        from .ollama_translate import OllamaTranslator
        return OllamaTranslator()
    if provider == 'google':
        return GoogleTranslator(keys.get_key('google'))
    if provider == "llm":
        return LLMTranslator()
    if provider == "deepl":
        return DeepLTranslator(keys.get_key("deepl"))
    raise StageError(f"未知的翻译服务：{provider}")


# ---------- WebVTT ----------

def _vtt_time(t: float) -> str:
    ms = max(0, int(round(float(t) * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _vtt_text(s: str | None) -> str:
    s = " ".join((s or "").split())  # 去掉换行，避免空行截断 cue
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_vtt(cues: list[dict], bilingual: bool) -> str:
    lines = ["WEBVTT", ""]
    for c in cues:
        lines.append(c["id"])
        lines.append(f"{_vtt_time(c['start'])} --> {_vtt_time(c['end'])}")
        lines.append(_vtt_text(c.get("text")))
        if bilingual and c.get("zh"):
            lines.append(_vtt_text(c["zh"]))
        lines.append("")
    return "\n".join(lines)


def write_vtts(dir_: Path, cues: list[dict]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for name, bi in (("subs.source.vtt", False), ("subs.bilingual.vtt", True)):
        tmp = dir_ / (name + ".tmp")
        tmp.write_text(build_vtt(cues, bi), encoding="utf-8")
        tmp.replace(dir_ / name)


# ---------- 阶段入口 ----------

def _save_cues(ctx: StageContext, cues: list[dict], cue_params: dict) -> None:
    artifacts.write_artifact(ctx.video_id, "cues.json", cues, cue_params)


def run(ctx: StageContext, translator: Translator | None = None) -> None:
    try:
        cues = ctx.read("cues.json")
    except FileNotFoundError as e:
        raise StageError("缺少 cues.json，请先完成字幕切分") from e
    old = (artifacts.read_artifact_meta(ctx.video_id, "cues.json") or {}).get("params") or {}
    if "provider" in old:  # 上次 translate 完成时写入的参数
        cue_params = ctx.settings.stage_params("cues")
        if json.loads(json.dumps(old)) != json.loads(json.dumps(ctx.params)):
            for c in cues:  # 翻译参数变了：重译
                c["zh"] = None
    else:
        cue_params = old or ctx.settings.stage_params("cues")

    p = ctx.settings.translate
    pending = [i for i, c in enumerate(cues) if not (c.get("zh") or "").strip() and (c.get("text") or "").strip()]
    total = len(pending)
    if total:
        translator = translator or make_translator(ctx.settings)
        k = p.context_cues
        done = 0
        ctx.progress(0.0, f"待翻译 {total} 条（{translator.name}）")
        batch_size = ctx.params.get('batch_size', p.batch_size)
        for b in range(0, total, batch_size):
            idxs = pending[b:b + batch_size]
            batch = [cues[i] for i in idxs]
            before = cues[max(0, idxs[0] - k):idxs[0]]
            after = cues[idxs[-1] + 1:idxs[-1] + 1 + k]
            result = translator.translate(batch, before, after, video_id=ctx.video_id)
            for i in idxs:
                cues[i]["zh"] = result[cues[i]["id"]]
            _save_cues(ctx, cues, cue_params)  # 断点：每批落盘
            done += len(idxs)
            ctx.progress(done / total, f"已翻译 {done}/{total} 条")

    write_vtts(ctx.dir, cues)
    ctx.write("cues.json", cues)  # 完成：带 translate 参数
    ctx.progress(1.0, f"字幕翻译完成，共 {len(cues)} 条")
