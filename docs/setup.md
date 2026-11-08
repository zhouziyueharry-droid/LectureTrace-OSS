# Setup and configuration

## Lightweight and full installations

Use Python 3.12, uv, and Node.js 22.18+ (24 LTS recommended). Install FFmpeg and ffprobe on PATH before extracting audio, processing video or running media-dependent tests.

The quick start in the README installs only the lightweight backend. New recordings cannot complete ASR until the optional dependencies are installed and `ASR_ENABLED=true` is configured. Installing the base environment does not download speech models.

Copy `.env.example` to `.env` only if you need overrides. API keys can be entered in Settings and stay in the local backend database; environment keys are a fallback. Never commit `.env` or `data/`.

By default, data is stored in the repository's `data/`. Set `DATA_DIR` to an absolute path to keep it elsewhere. Check the startup log before switching installations. Back up the entire data directory while the application is stopped.

## CPU transcription

```sh
uv sync --directory backend --locked --extra asr
```

Set these values in `.env`:

```dotenv
ASR_ENABLED=true
ASR_DEVICE=cpu
ASR_COMPUTE_TYPE=int8
ASR_BATCH_SIZE=0
ASR_MODEL=small
```

The first real ASR run may download the configured model. `large-v3` is the application's default; a smaller model uses fewer resources with different accuracy. You can point `ASR_MODEL` to a local faster-whisper/CTranslate2 model directory.

## NVIDIA GPU transcription

```sh
uv sync --directory backend --locked --extra cuda
```

Set `ASR_ENABLED=true`, `ASR_DEVICE=cuda`, `ASR_COMPUTE_TYPE=float16` and `ASR_BATCH_SIZE=16`. A compatible NVIDIA driver is required. Linux may also need the installed NVIDIA library directories in `LD_LIBRARY_PATH`; follow the [faster-whisper requirements](https://github.com/SYSTRAN/faster-whisper#gpu).

When syncing an existing full environment, keep the original extras on the command. Use `uv run --directory backend --no-sync ...` to run without removing optional dependencies.

## Translation, outline and Q&A

Select services in Settings. DeepSeek Flash has a separate key and is restricted to slide reading and outline generation; it does not replace the existing Q&A or subtitle-translation connection. Model availability and account access are provider-specific.

Ollama translation connects only to `http://127.0.0.1:11434` and expects the local model `hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M`. Install Ollama and download that model separately; its license and hardware requirements are independent of this project. This path translates English subtitles into Simplified Chinese.

Transient local generation failures (`ConnectError`, `ConnectTimeout`, or HTTP 500 containing `dial tcp 127.0.0.1:` / `model runner has unexpectedly stopped`) retry the same subtitle up to five attempts, including the first, with 2/4/6/8-second waits. Other HTTP errors, read timeouts, invalid JSON, and empty/truncated output do not retry. Model-list checks still make one request. Completed subtitles are saved individually; after retries are exhausted, resume from the checkpoint using Retry. Logs report attempt counts and delays without raw error bodies or subtitle text.

API translation/reading/outline/Q&A can incur provider charges. A connection check does not verify generation quality or available balance. Do not use real calls in automated tests.

Codex Q&A is optional and requires a compatible Codex CLI and login through the application's settings. Its course workspace must be outside the project directory. API Q&A remains available without the Codex runtime. Runtime capability checks, account access and model entitlements are separate from this repository's MIT license.

## PDF OCR

Native PDF text extraction and page rendering work in the base install. Scanned-page OCR is optional:

```sh
uv sync --directory backend --locked --extra pdf-ocr
uv run --directory backend --no-sync python ../scripts/prepare_pdf_models.py
```

Combine `--extra pdf-ocr` with your existing ASR/CUDA extra if applicable. Model preparation downloads files into `models/docling/`; the OCR worker uses the prepared local models. Normal application startup does not prepare them automatically.

## Development

Start the backend:

```sh
uv run --directory backend --no-sync python -m app.main
```

In another terminal:

```sh
npm --prefix frontend run dev
```

Visit http://127.0.0.1:5173. Vite proxies `/api` to port 8000. Restart the backend after Python changes. Rebuild the frontend to update the page served directly on port 8000.

## Processed course packages

Use the lecture menu or course menu to export `.ltcourse`; import it through the import dialog. Packages contain course content, videos, notes and PDFs and may be large. They do not include credentials or model weights. Duplicate videos are rejected rather than overwritten. Only share packages whose content you are allowed to distribute.
