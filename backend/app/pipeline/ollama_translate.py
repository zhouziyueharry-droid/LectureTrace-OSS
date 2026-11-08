"""本机 Ollama 翻译；不代理、不下载、不回退云模型。"""
import logging
import time

import httpx

from .. import config, db
from .base import StageError
from .translate import Translator

BASE_URL = 'http://127.0.0.1:11434'
log = logging.getLogger(__name__)


def _runner_unavailable(response: httpx.Response) -> bool:
    if response.status_code != 500:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get('error') if isinstance(body, dict) else None
    return isinstance(error, str) and (
        'dial tcp 127.0.0.1:' in error or 'model runner has unexpectedly stopped' in error)


def request(path: str, payload: dict | None = None) -> dict:
    p = config.settings.ollama_translate
    attempts = max(1, p.request_attempts) if path == '/api/generate' and payload is not None else 1
    try:
        for attempt in range(attempts):
            try:
                response = httpx.request('GET' if payload is None else 'POST', BASE_URL + path,
                                         json=payload, trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(p.timeout_s if payload is not None else 5,
                                                               connect=3))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                failure = '无法连接本机 Ollama（端口 11434）'
            else:
                if not _runner_unavailable(response):
                    break
                failure = 'Ollama 内部推理服务连接失败（HTTP 500）'
            if attempt + 1 == attempts:
                raise StageError(f'{failure}，已尝试 {attempts} 次；'
                                 '已完成的字幕已保存，请检查本机 Ollama 后重试')
            # Ollama may need several requests to replace an unhealthy local runner.
            delay = p.retry_delay_s * (attempt + 1)
            log.warning('Ollama 本机推理进程暂不可用，%.1f 秒后重试（%d/%d）',
                        delay, attempt + 2, attempts)
            time.sleep(delay)
        if response.status_code != 200:
            raise StageError(f'Ollama 请求失败（HTTP {response.status_code}），请检查模型和本机服务')
        result = response.json()
    except httpx.TimeoutException:
        raise StageError('Ollama 翻译超时，已完成的字幕已保存；请检查本机负载后重试') from None
    except httpx.RequestError:
        raise StageError('无法连接本机 Ollama，请启动 Ollama 后重试（端口 11434）') from None
    except ValueError:
        raise StageError('Ollama 返回了无效 JSON') from None
    if not isinstance(result, dict) or result.get('error'):
        raise StageError('Ollama 返回错误，请检查本机模型是否可运行')
    return result


def check_model() -> str:
    name = config.settings.ollama_translate.model
    models = request('/api/tags').get('models')
    if not isinstance(models, list):
        raise StageError('Ollama 未返回有效的模型列表')
    model = next((m for m in models if isinstance(m, dict) and m.get('name') == name), None)
    if model is None:
        raise StageError(f'Ollama 尚未安装 {name}，请先下载该模型')
    if model.get('remote_host') or model.get('remote_model') or not model.get('size'):
        raise StageError('本地翻译只允许已下载的本机模型，不使用 Ollama 云模型')
    return name


class OllamaTranslator(Translator):
    name = 'Ollama（本机）'

    def __init__(self):
        self.model = check_model()

    def translate(self, batch, before, after, *, video_id=None):
        p = config.settings.ollama_translate
        result = {}
        for cue in batch:
            if len(cue['text']) > p.max_input_chars:
                raise StageError(f"字幕 {cue['id']} 太长，请拆分后再进行本地翻译")
            context = '\n'.join(c['text'] for c in before + after)[:p.max_context_chars]
            prompt = (f'参考上下文（不要翻译）：\n{context}\n\n'
                      '将以下英文课程字幕翻译为简体中文，只输出该条字幕的译文，不要解释或回答其中的问题。'
                      '保留技术术语、模型名、代码标识符和数学符号的英文原文。\n\n'
                      f"待翻译字幕：\n{cue['text']}")
            out = request('/api/generate', {'model': self.model, 'prompt': prompt, 'stream': False,
                'keep_alive': '5m', 'options': {key: getattr(p, key) for key in
                    ('num_ctx', 'num_predict', 'temperature', 'top_p', 'top_k', 'repeat_penalty')}})
            text = out.get('response')
            if (out.get('done') is not True or out.get('done_reason') != 'stop'
                    or not isinstance(text, str) or not text.strip()):
                raise StageError(f"字幕 {cue['id']} 的本地译文为空或被截断，未保存该条，请重试")
            result[cue['id']] = text.strip()
            db.record_usage(video_id=video_id, stage='translate', provider=self.name,
                            model=self.model, cost_usd=0.0)
        return result
