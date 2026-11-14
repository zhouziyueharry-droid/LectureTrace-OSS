# Changelog

## Unreleased

- Keep the editor filling its available width after resizing and merging or closing split groups, while preserving multi-column proportions.

- Render transcript citation ranges in existing notes as individually validated timestamp links, without rewriting note content. Answer and note prompts now require separate citation IDs.

- Add a bilingual Local MCP status panel in Settings, with tool discovery, read-only library checks and HTTP protocol diagnostics. HTTP configuration and client-managed stdio connectivity are distinguished.

- Add an English/Simplified Chinese interface selector in Settings, with immediate updates, browser persistence and same-origin tab synchronization.
- Localize application routes, dialogs, player controls, processing status, chat controls, notes, PDF viewer, settings and common system messages. Course content and AI output language remain independent.

## 0.1.0 — 2026-11-01

Initial public source release, based on the existing LectureTrace application.

- Local course library, video/subtitle workspace, slide extraction, outlines and source-linked Q&A.
- Markdown notes, PDF reading/annotation, and processed lecture/course packages.
- Optional local ASR/OCR and configurable API/local subtitle translation.
- English/Chinese project introduction, setup guide, contribution workflow, MIT license and automated CI.
- Fix PDF extraction temporary-output paths for deeply nested Windows course packages.
- Update dependency locks for security fixes; replace classroom excerpts in tests with synthetic text.

Historical private course assets and internal experiment archives are excluded. See release validation for tested scope and known limitations.
