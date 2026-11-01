# LectureTrace

**Turn local lecture recordings into a workspace you can trace back to the source.**

[简体中文](docs/README.zh-CN.md) · [Setup](docs/setup.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

![CI](https://github.com/zhouziyueharry-droid/LectureTrace-OSS/actions/workflows/ci.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Founder and primary maintainer:** [Harry (@zhouziyueharry-droid)](https://github.com/zhouziyueharry-droid).

LectureTrace is a local desktop-browser application for studying recorded lectures. Watch the video alongside subtitles, slides, notes and PDFs; follow timestamps and citations back to the material when checking an answer. The current interface is primarily Simplified Chinese.

## What it does

- Organize local MP4 recordings into courses.
- Generate timestamped subtitles with optional local Whisper ASR; translate subtitles with DeepL, Google, an API model, or local Ollama.
- Extract slides from a user-confirmed region and generate a timestamped outline.
- Ask questions with source references through an API model or an optional Codex runtime.
- Edit Markdown notes, read and annotate PDFs, and work in a resizable reading workspace.
- Export and import processed lectures or complete courses as `.ltcourse` packages.

The backend uses Python, FastAPI and SQLite. The frontend uses React, TypeScript and Vite. Your recordings and database stay in a local data directory. **Local storage does not mean every feature is offline:** API translation, slide reading, outline generation and model Q&A send selected content to the configured provider. Check the send plan and estimated costs before confirming.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node.js](https://nodejs.org/) 22.18+ (Node 24 LTS recommended). Python 3.12 is pinned in `backend/.python-version`; uv can install it. Install FFmpeg and ffprobe for video/audio processing.

```sh
git clone https://github.com/zhouziyueharry-droid/LectureTrace-OSS.git
cd LectureTrace-OSS
uv sync --directory backend --locked
npm ci --prefix frontend
npm --prefix frontend run build
uv run --directory backend --no-sync python -m app.main
```

Open **http://127.0.0.1:8000**. No API key or `.env` file is needed to launch the UI.

The default installation is **lightweight**: it does not install Whisper, CUDA or OCR models. It can manage notes/PDFs and read imported processed course packages. Processing a new recording needs optional ASR plus your chosen services; unavailable capabilities produce an explicit error. See [full setup](docs/setup.md) for CPU/GPU ASR, local translation, PDF OCR and developer mode.

## Contribute

Issues and pull requests are welcome in English or Chinese. You do not need collaborator access: fork this repository, create a branch, and open a PR. Start with a reproducible bug, a focused usability improvement, documentation, or a test.

```sh
uv run --directory backend --no-sync pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Tests use temporary data and mocked model services. Never add keys, recordings, course slides or private datasets to a PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for attribution, review and validation expectations.

**NTU AI6130 group members (26S1, Large Language Models):** please read the [group collaboration notes](docs/AI6130-GROUP.md#english) for our assignment workflow, access after the course, continued participation and contributor credit.

## Status and limitations

This is an early public source release of an existing personal project. The public history starts with a reviewed source snapshot; private lecture materials and historical experiment artifacts are not included. Future contributions will be recorded through public commits and PRs.

- Designed for one trusted local user; **not a hosted multi-user service**. Keep the server on loopback.
- Model answers and extracted measurements can be wrong. Verify against the source material.
- Provider model availability and prices vary. Built-in estimates are dated approximations, not billing guarantees.
- Optional GPU, OCR and real provider calls need your own setup. Automated tests do not establish their end-to-end quality.
- PDF rendering includes large assets; the production build may report bundle-size warnings.

[Architecture](ARCHITECTURE.md) · [Release validation](docs/release-validation.md) · [Changelog](CHANGELOG.md)

## License

LectureTrace project code is available under the [MIT License](LICENSE), including commercial use. Dependencies and optional model weights retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). The project license does not grant rights to recordings, slides, PDFs or other content you import.
