# Third-party notices

The MIT License in this repository covers LectureTrace project code. It does not replace the licenses of dependencies, optional model weights or imported content. No lecture recordings, slides, PDFs or pretrained model weights are distributed in this source repository.

Dependencies are resolved in `frontend/package-lock.json` and `backend/uv.lock`. Installed packages include their own copyright and license files. Preserve required notices when distributing built applications or vendored dependencies; this document is a navigation aid, not a replacement for those texts.

| Component | License / source |
| --- | --- |
| React, Vite, KaTeX, remark/rehype packages | MIT; see individual installed packages |
| TypeScript | Apache-2.0 |
| EmbedPDF React viewer and plugins | MIT; see package LICENSE files |
| PDFium | BSD-style license and bundled third-party notices |
| Noto Sans fonts | SIL Open Font License 1.1 |
| Lightning CSS and platform binaries | MPL-2.0 |
| FastAPI, uvicorn, pydantic, httpx | See each installed package's MIT/BSD-style license |
| pypdf | BSD-3-Clause |
| pypdfium2 | See package license and bundled PDFium third-party notices |
| Pillow | HPND and bundled third-party notices |
| NumPy, SciPy, scikit-image | BSD-style licenses and bundled third-party notices |
| FFmpeg | External system dependency; licensing depends on the selected build |

Optional Whisper, Ollama/HY-MT2, Docling and OCR models are installed separately. Check the exact model card and distribution terms before redistributing weights or using them commercially. API services and Codex account access are governed by the provider's own terms.
