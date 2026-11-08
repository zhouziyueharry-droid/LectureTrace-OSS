"""读取 .env 与可调参数。所有模块只从这里拿配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AsrParams:
    model: str = os.getenv("ASR_MODEL", "large-v3")
    device: str = os.getenv("ASR_DEVICE", "cuda")
    compute_type: str = os.getenv("ASR_COMPUTE_TYPE", "float16")
    # 批量识别：按语音段分批同时送进 GPU，实测约快 3 倍，文字与顺序识别基本一致；0 表示顺序识别
    batch_size: int = int(os.getenv("ASR_BATCH_SIZE", "16"))
    # 轻量开发默认关闭；安装 asr / cuda extra 后显式开启才允许下载、加载本地模型。
    enabled: bool = os.getenv("ASR_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class CueParams:
    max_chars: int = 84
    max_s: float = 7.0
    min_s: float = 1.0


@dataclass(frozen=True)
class KeyframeParams:
    interval_s: float = 1.0
    width: int = 320
    stable_s: float = 3.0
    diff_threshold: float = 1.0       # 变化像素占比（%），超过视为画面变化
    phash_max_distance: int = 6       # v2 起不再使用，保留字段避免旧产物 params 解析出错
    # 手工遮罩矩形（相对坐标 x, y, w, h，0~1），例如讲师小窗
    masks: tuple[tuple[float, float, float, float], ...] = ()
    # ---- v2（提案 0001 交接 §3）；阈值只在测试课上验证过，需跨视频校准 ----
    block_grid: tuple[int, int] = (16, 9)  # 分块比对的（列, 行）
    block_tol: float = 0.02          # 块内变化像素占比 ≤ 此值，视为该块未变
    dup_threshold: float = 0.96      # 未变块占比 ≥ 此值，视为同一页（测试课：不同页最高 0.903；翻回加批注 0.965）
    reveal_min_ink: float = 3.0      # 逐条展开：旧帧内容像素 ≥ 3%（内容框内）
    reveal_max_added: float = 2.0    # 逐条展开：新增 / 旧内容 ≤ 2.0
    blank_ink: float = 0.5           # 内容像素 < 此值(%)不算幻灯片（测试课：Matrix 标题页 0.648，End of slide show 0.34，余量小）
    zoom_enabled: bool = False       # 可选功能 E：放大视图挂到父页
    zoom_min_scale: float = 1.3


@dataclass(frozen=True)
class SegmentParams:
    """讲解片段切分（交接 §4.1）。取代 v1 的 EvidenceParams.segment_s。"""
    gap_absorb_s: float = 15.0     # 出现区间之间的空档 < 此值时并入前一页（只延长归属窗口）
    silence_split_s: float = 20.0  # cue 间静音 > 此值时硬切 T（只切 T，不切出现）
    sentence_snap_s: float = 5.0   # 换页落在句中时，允许延后等句末
    t_target_s: float = 30.0
    t_max_s: float = 90.0
    t_min_tail_s: float = 10.0


@dataclass(frozen=True)
class OllamaTranslateParams:
    model: str = 'hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M'
    num_ctx: int = 8192
    num_predict: int = 2048
    temperature: float = 0.7
    top_p: float = 0.6
    top_k: int = 20
    repeat_penalty: float = 1.05
    timeout_s: int = 180
    max_input_chars: int = 2000
    max_context_chars: int = 1000


@dataclass(frozen=True)
class TranslateParams:
    provider: str = os.getenv("TRANSLATOR", "deepl")  # "deepl" | "google" | "llm" | "ollama"
    context_cues: int = 2
    batch_size: int = 40


@dataclass(frozen=True)
class SlideParams:
    batch_size: int = 5
    max_edge: int = 1280
    max_speech_chars: int = 6000   # 每个出现附带讲解的字符上限，按整段 T 截断（交接 §5.1）
    split_retry: bool = True       # 多页批次中失败的页拆成单页各重试一次
    # 输出语言的本地软检查（只对英语课程）；来源：v2 实验 result_v2.json，英文摘要虚词比例 0.30–0.45，
    # Sol 误写成西班牙语的 5 条为 0.000–0.024。详见 slides.py「输出语言的本地检查」
    lang_min_function_words: float = 0.10  # 摘要中英语虚词占比低于此值视为不是英语
    lang_max_foreign_summary: float = 0.20  # 摘要中非英语字母占比高于此值视为不是英语（抓中文等其他文字）
    lang_max_foreign_visual: float = 0.50   # 画面内容放宽（幻灯片本身可能夹带其他文字）
    lang_min_words: int = 20                # 词数不足时不判断
    lang_min_letters: int = 40              # 字母数不足时不判断


@dataclass(frozen=True)
class QaParams:
    """问答两步流程的展开上限（交接 §10.2）。"""
    max_expand_ids: int = 12
    max_expand_images: int = 2
    max_expand_t: int = 40
    max_expand_chars: int = 20000


@dataclass(frozen=True)
class CodexQaParams:
    """课程检索显示参数；max_* 仅兼容旧配置，不再限制 Codex 读取。"""
    max_tool_calls: int = 30
    max_read_tokens: int = 12000
    max_images: int = 4
    search_k: int = 8
    snippet_chars: int = 200


@dataclass(frozen=True)
class NoteParams:
    max_bytes: int = 512_000
    max_name_chars: int = 120
    # 版本历史：同一作者（用户）在窗口内的连续保存合并为一个版本
    max_versions: int = 40
    version_window_s: int = 300
    trash_days: int = 30


@dataclass(frozen=True)
class PackageParams:
    max_archive_bytes: int = 32 * 1024**3
    max_expanded_bytes: int = 64 * 1024**3
    max_files: int = 20_000
    max_metadata_bytes: int = 32 * 1024**2
    max_manifest_bytes: int = 8 * 1024**2
    ttl_s: int = 24 * 3600


@dataclass(frozen=True)
class PdfParams:
    max_bytes: int = 32 * 1024**2
    max_pages: int = 500
    max_text_bytes: int = 4 * 1024**2
    max_stream_bytes: int = 8 * 1024**2
    timeout_s: int = 60
    max_read_pages: int = 5
    image_long_edge: int = 1600
    ocr_long_edge: int = 2200
    max_render_pixels: int = 12_000_000
    max_render_scale: int = 32
    max_image_bytes: int = 4 * 1024**2
    max_cache_bytes: int = 64 * 1024**2
    worker_timeout_s: int = 120
    worker_wait_s: int = 10
    worker_threads: int = 2
    docling_models_dir: str = str(ROOT_DIR / 'models' / 'docling')


@dataclass(frozen=True)
class McpParams:
    page_size: int = 50
    max_queries: int = 8
    max_query_chars: int = 1000
    text_chunk_chars: int = 24000
    summary_chars: int = 600
    max_request_bytes: int = 65536


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data")))
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    deepl_api_key: str = os.getenv("DEEPL_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")   # 仅 anthropic 服务商使用
    # 读取幻灯片、生成大纲、问答走哪个服务商："openai"（OpenAI 兼容接口，模型在设置页选）或 "anthropic"
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai")
    # 强制停用的阶段（调试用）。读幻灯片、整理证据、生成大纲是否执行由设置页的"自动生成大纲"开关
    # 或视频页的"运行大纲生成"决定，见 runner.is_disabled
    disabled_stages: tuple[str, ...] = tuple(
        s.strip() for s in os.getenv("DISABLED_STAGES", "").split(",") if s.strip())
    asr: AsrParams = field(default_factory=AsrParams)
    cues: CueParams = field(default_factory=CueParams)
    keyframe: KeyframeParams = field(default_factory=KeyframeParams)
    segment: SegmentParams = field(default_factory=SegmentParams)
    translate: TranslateParams = field(default_factory=TranslateParams)
    slides: SlideParams = field(default_factory=SlideParams)
    qa: QaParams = field(default_factory=QaParams)
    codex_qa: CodexQaParams = field(default_factory=CodexQaParams)
    notes: NoteParams = field(default_factory=NoteParams)
    packages: PackageParams = field(default_factory=PackageParams)
    pdfs: PdfParams = field(default_factory=PdfParams)
    google_translate_api_key: str = os.getenv("GOOGLE_TRANSLATE_API_KEY", "")
    ollama_translate: OllamaTranslateParams = field(default_factory=OllamaTranslateParams)
    mcp: McpParams = field(default_factory=McpParams)
    mcp_token: str = os.getenv("LECTURETRACE_MCP_TOKEN", "")

    def translation_provider(self) -> str:
        from . import db
        return (db.get_setting("translation_provider") or self.translate.provider or "deepl").lower()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    def _llm_params(self, purpose: str) -> dict:
        from .llm import settings as llm_settings

        if llm_settings.uses_deepseek(purpose):
            return {"provider": "deepseek", "model": llm_settings.DEEPSEEK_MODEL}
        if self.llm_provider != "openai":
            return {"provider": self.llm_provider, "model": self.llm_model}
        from .llm import settings as llm_settings  # 延迟导入，避免循环依赖

        return {"provider": "openai", "model": llm_settings.model_for(purpose)}

    def _output_language(self) -> str:
        from .llm import settings as llm_settings  # 延迟导入，避免循环依赖

        return llm_settings.get()["output_language"]

    def stage_params(self, stage: str) -> dict:
        """每个阶段写入产物 params 的内容；变化即触发该阶段重跑。"""
        if stage == "translate":
            provider = self.translation_provider()
            if provider == 'ollama':
                return {**asdict(self.translate), **asdict(self.ollama_translate),
                        'provider': provider, 'batch_size': 1}
            if provider == "google" or provider != self.translate.provider:
                return {**asdict(self.translate), "provider": provider,
                        "model": self.llm_model if provider == "llm" else None}
        return {
            "extract_audio": {},
            # 设备、精度与批量大小不影响结果内容，只比较模型名，切换它们不触发重跑
            "asr": {"model": self.asr.model},
            "cues": asdict(self.cues),
            "keyframes": asdict(self.keyframe),
            "segments": asdict(self.segment),
            "translate": {**asdict(self.translate),
                          "model": None if self.translate.provider == "deepl" else self.llm_model},
            "read_slides": {**asdict(self.slides), **self._llm_params("read_slides")},
            "evidence": {},  # v2 只做组装，无可调参数（交接 §6）；切分参数在 segments
            "outline": {**self._llm_params("outline"), "output_language": self._output_language()},
        }.get(stage, {})


settings = Settings()
