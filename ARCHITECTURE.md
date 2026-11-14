# Architecture

Architecture revision: MCP slide location and progressive units, 2026-11-08.

LectureTrace is a single-process local application. FastAPI serves the API and, after a frontend build, the React application. SQLite stores course, stage, conversation and settings records; file artifacts live under the configured local data directory.

## 1. Application boundaries

- `backend/app/main.py`: API registration, startup and frontend serving.
- `backend/app/api/`: HTTP interfaces for courses, recordings, processing, settings, notes, PDFs and chat.
- `backend/app/config.py`, `db.py`, `artifacts.py`: configuration, persistence and versioned artifact access.
- `frontend/src/pages/`: library, recording workspace and settings.
- `frontend/src/components/`: playback, editors, PDF views, chat and processing controls.

## 2. Source and storage

Each recording owns a directory containing its original media and derived artifacts. JSON artifacts carry a schema version and processing parameters. Changed parameters invalidate affected results. Local files, keys and databases are excluded from Git.

## 3. Processing pipeline

```mermaid
flowchart LR
  A[Local MP4] --> B[Audio extraction]
  B --> C[Optional local ASR]
  C --> D[Timestamped subtitle cues]
  A --> E[Confirmed slide region]
  E --> F[Keyframes and appearances]
  D --> G[Speech segments]
  F --> G
  D --> H[Subtitle translation]
  G --> I[Slide reading and evidence]
  I --> J[Timestamped outline]
```

`pipeline/runner.py` owns the stage queue, checkpoints, failure state and resumption. CPU/image stages operate locally. External model stages use `llm/` and the user's configured provider. `pipeline/send_plan.py` exposes the proposed content and approximate costs before confirmation. Ollama subtitle translation uses a local service.

Local Ollama generation retries connection failures and HTTP 500 responses identifying a stopped runner or a loopback connection failure. The same subtitle gets at most five attempts with 2/4/6/8-second delays; other failures remain explicit. Per-cue checkpoints preserve completed translations. These retry parameters are included in artifact parameters; explicitly rerunning a completed translation with changed parameters retains the existing retranslation behavior. This tolerates transient failures, but does not resolve the underlying Ollama/Windows runner instability.

## 4. Q&A and editing

API Q&A and optional Codex Q&A live under `qa/` and `codex/`. Persistent chat runs separate backend work from a connected browser observer so refreshes can recover state. Notes support history and suggestions. PDF references bind a document identity, revision and page; extracted text is not a guarantee of complete visual understanding.

## 5. Artifacts

Key artifacts include `transcript.json`, `cues.json`, subtitle VTTs, `keyframes.json`, `segments.json`, `slides.json`, `evidence.json` and `outline.json`. Markdown notes and immutable source PDFs have their own metadata. Historical inline section references in source comments refer to the original internal design; executable contracts are defined by code and tests in this public snapshot.

### Interface language

`frontend/src/i18n.ts` owns the `zh-CN` / `en` interface preference, stored as `lt.locale` in browser localStorage (default Simplified Chinese). React components subscribe with `useLocale`; switching updates mounted views and the document language without keying or remounting the workspace. Storage events synchronize same-origin tabs. Storage failures retain a session-only preference. Settings exposes the selector independently of backend configuration loading.

`tr` resolves application copy and positional values from `locales/en.ts`. `systemText` translates known backend status/error templates at display boundaries using `locales/system-en.ts`, preserving inserted names and raw unknown diagnostics. Course text, notes, model answers, citation identifiers and persisted artifacts are not translated. The PDF viewer receives the current locale through its existing i18n plugin. AI output language remains a separate backend setting; no API or data schema changes are needed. See [implementation and validation](docs/proposals/0031-handoff.md).

## 6. Packages and trust

`course_packages.py` exports/imports versioned ZIP-based `.ltcourse` packages. Validation checks file names, hashes, sizes and metadata; imports reject duplicates and roll back on failure. Packages contain content, not application credentials. PDF processing uses bounded subprocesses. This architecture does not make the application a sandbox for arbitrary hostile workloads or a multi-user hosted service.

## 7. Read-only MCP

Settings exposes a Local MCP status card through `api/mcp_status.py`. The read-only `/api/mcp/status` probe uses the lifespan-owned `McpHTTP` instance stored on FastAPI state; it never imports the web entry point from the stdio server. With HTTP enabled it exercises the token gate and JSON-RPC initialization, tool discovery and `list_videos` through an in-process ASGI client. With HTTP disabled it checks the same initialized server's tools and library directly. Results separate HTTP availability from library readability and never include credentials or course metadata. This is not a monitor of external clients, tunnels or independently launched stdio processes.

The optional `mcp` extra adds `app/mcp_server.py`. Standard clients use either stdio (`python -m app.mcp_server`) or the web backend's Streamable HTTP `/mcp` endpoint. Both expose the same ten tools and use the existing DATA_DIR. MCP never invokes the Codex runtime or another model. Stdio opens SQLite read-only and does not initialize the database or start a processing worker. HTTP shares the web application's normal lifecycle; its processing worker remains responsible for any existing web jobs.

`qa/evidence_reader.py` owns shared search, text expansion and image selection. The web `CourseWorkspace` subclasses it and retains its note/workspace migration, read logging and final-citation validation. MCP uses the reader without those side effects, reads outlines without migration, and returns evidence versions and source labels. Clients' generated answers are not automatically validated by the web citation checker.

`qa/citations.py` and `CitedMarkdown` also accept legacy transcript ranges such as `[T0152-T0154]` (hyphen, en dash or em dash), including grouped citations. Each ascending range expands to at most 1,000 individual IDs; missing/unread IDs still fail server validation, and stale evidence stays inactive. Notes resolve citations when read and expand them for display without changing stored Markdown or revisions. Editing continues to show the original video-citation syntax. Answer and workspace prompts require one ID per bracket rather than ranges.

HTTP requires a local bearer token and the SDK's Host/Origin checks. An unset token disables HTTP access; stdio relies on local process permissions. Synchronous file/query work runs in a thread so it does not block the web event loop. Lists, context and text have continuation fields; tool output includes standard text (JSON fallback), structured content, and JPEG image content. No database schema, course artifact format or pipeline parameter changes are required. See [MCP setup and contracts](docs/mcp.md) and [validation](docs/proposals/0027-experiments/README.md).

### Progressive video-unit access

For whole-lecture text Q&A, `read_video_transcript` directly reads the final ASR `transcript.json`, preserving original text and millisecond start/end timestamps. It returns the entire formatted transcript up to 160,000 characters, with explicit continuation beyond that. `view_keyframe` independently reads `keyframes.json` and stored images by half-open display interval; repeated appearances retain their own screenshots, overlaps paginate one image at a time, and gaps do not select a nearest image. These tools require no translation, slide reading, segments, outline or evidence. Source fingerprints protect continuation from mixed revisions; their versions are independent of each other and of evidence versions. They neither extract new frames nor alter pipeline artifacts. See [0029](docs/proposals/0029-full-transcript.md).

`qa/slide_locator.py` matches a client-provided PNG/JPEG against a selected video's existing screenshots using SIFT and RANSAC. It only locates candidate timestamps and section IDs. No query images, page mappings, or index files are persisted; a bounded in-process feature cache accelerates repeated queries. Query byte/pixel limits and geometric thresholds live in `SlideMatchParams`. Native dependencies load before stdio starts its read loop to avoid Windows worker-thread initialization stalls.

```mermaid
flowchart LR
  A[Client-rendered PPT page] --> B[locate_slide: candidate timestamps]
  B --> C[get_video_units: existing summaries]
  C --> D{Agent needs more?}
  D --> E[read_video_unit: all assigned transcript chunks]
  D --> F[view_slide: original screenshot]
```

A unit is an existing continuous appearance (`S012.1`) or no-slide section (`N001`), not a new semantic segmentation. Timestamp lookup considers half-open display and speech intervals and preserves overlapping boundaries. Previews disclose missing/partial generated summaries without loading transcript bodies into tool responses. `read_video_unit` expands the exact `t_ids` set and exposes text continuation; missing members are errors. Existing `read_evidence` retains its original page/summary/speech semantics. The web already has short-index navigation; this addition provides the missing explicit unit workflow in MCP without replacing the web runtime. See [0028 validation](docs/proposals/0028-experiments/README.md).

### Independent Sites course reader

The public classroom MCP now runs as a separate nine-tool Node service on Render at `https://lecturetrace-course-mcp.onrender.com/mcp`, with authentication None. Sites retains the explanatory homepage, authenticated owner-plugin endpoint, R2 storage and administrator-only import API. Render reads cloud artifacts via GET-only `/data-read/*` with a dedicated `DATA_READ_TOKEN`; it has no import token, Sites service token, host credentials, model keys or tunnel. Its HTTP adapter exposes only MCP, health and homepage redirect; administrative routes are unreachable. Catalog/manifests refresh within 60 seconds, while content-addressed snapshots/images use a 64 MiB memory cache. Imports still validate images and snapshots before atomically replacing a course manifest, preserving full transcript/keyframe payloads and available evidence/outline. Image reverse lookup is disabled; the separate Python/OpenCV Render service remains suspended. Free Render can sleep and cold-start. The existing local ten-tool MCP and processing workers are unchanged. See [0030](docs/proposals/0030-sites-course-mcp.md).
