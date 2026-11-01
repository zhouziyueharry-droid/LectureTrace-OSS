# Initial public release validation

Recorded on 2026-11-01 for the initial source snapshot. Results describe the checks below, not a security certification or real-model quality benchmark.

## Local checks

Environment: Windows, Python 3.12.13, a fresh uv base installation and a fresh npm installation. No optional ASR/CUDA/OCR extras were installed. Tests use temporary databases, synthetic media and mocked model services.

| Check | Result |
| --- | --- |
| Backend suite | 556 passed; one expected warning from a deliberately duplicated ZIP entry |
| Frontend tests | 23 passed |
| TypeScript and production build | Passed; PDF assets produce bundle-size and browser `crypto` externalization warnings |
| Fresh-data startup smoke | `/`, `/api/courses`, `/api/videos` returned HTTP 200; course and video lists were empty |
| Base Python environment, pip-audit | No known vulnerabilities reported at check time |
| npm audit | 0 critical, 0 high, 0 moderate; 4 low package entries from the KaTeX advisory below |

The smoke check used an isolated temporary directory and no provider credentials. No paid API requests, real transcription, model downloads or real Codex login were used for this validation. macOS and optional GPU/OCR packages have not been validated for this snapshot. CI runs backend tests on Linux and Windows, and frontend tests/build on Linux; see the actual [workflow runs](https://github.com/zhouziyueharry-droid/LectureTrace-OSS/actions/workflows/ci.yml) for their current outcomes.

## Corrections made while preparing this snapshot

- PDF extraction now writes worker output to a short independent temporary directory. A deep course-package staging directory could otherwise exceed the Windows path limit when a longer extraction filename was appended. A regression test checks deep-path extraction, original-file preservation and absence of extraction debris.
- Every backend test gets an initialized temporary settings database, including tests that previously relied on a developer's existing database. Provider keys are cleared in this fixture.
- The frontend HTTP cache test creates its own tiny static fixture, so it works before a frontend build and is not skipped on a fresh clone.
- Vite, source-map-js, Pillow, pypdf and certifi were updated to resolve reported advisories. Resolving Pillow compatibility also changed optional Docling lock entries; those extras were not exercised.
- Small classroom text excerpts in evidence tests were replaced with invented text while preserving structural cases and assertions.

## Remaining dependency advisory

[`GHSA-238p-pmpm-9mq7`](https://github.com/advisories/GHSA-238p-pmpm-9mq7) describes a KaTeX trust-restriction bypass in the presence of existing prototype pollution. npm reports it as low severity and lists KaTeX plus its dependent math packages (four package entries, one underlying advisory).

The current math stack uses KaTeX 0.16.47; the advisory lists 0.18.2 as fixed. Updating the complete stack requires compatibility review because the current wrappers constrain KaTeX to the older series. This release does not force an incompatible override. CI blocks high/critical npm findings and reports the full audit; this low-severity finding remains open. Keep the application local and treat imported content as untrusted.

## Public content boundary

The initial public commit contains application source, synthetic tests, dependency locks and new contributor documentation. It excludes the private development history, historical experiment screenshots, lecture slides/recordings, datasets, databases, credentials, model weights and local environments. Pattern-based credential checks and file review reduce accidental disclosure risk but cannot prove that a codebase is free of every secret or vulnerability.
