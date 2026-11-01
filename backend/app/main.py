"""FastAPI 入口：挂载路由，启动后台工作线程；生产环境托管 frontend/dist。

启动：cd backend && uv run python -m app.main
  或：cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import db
from .api import packages
from .api import pdfs
from .api import chat, codex, courses, notes, outline_edits, processing, settings, videos
from .config import ROOT_DIR, settings as app_settings
from .pipeline import runner

HOST, PORT = "127.0.0.1", 8000
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger(__name__).info("Code root: %s; data directory: %s",
                                     ROOT_DIR, app_settings.data_dir.resolve())
    db.init_db()
    runner.start_worker()
    try:
        yield
    finally:
        from .codex.runtime import close_runtime
        close_runtime()


app = FastAPI(title="LectureTrace", lifespan=lifespan)
app.include_router(packages.router)
app.include_router(videos.router)
app.include_router(courses.router)
app.include_router(processing.router)
app.include_router(chat.router)
app.include_router(settings.router)
app.include_router(codex.router)
app.include_router(outline_edits.router)
app.include_router(notes.router)
app.include_router(pdfs.router)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """统一错误格式 {"detail": "中文原因"}。"""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        parts.append(f"{loc}：{err.get('msg', '')}" if loc else err.get("msg", ""))
    return JSONResponse({"detail": "请求参数错误：" + "；".join(parts)}, status_code=422)


def _mount_frontend() -> None:
    """frontend/dist 存在时托管静态文件，未知路径回退到 index.html（SPA）。"""
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        return
    root = FRONTEND_DIST.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root) and target != index.resolve():
            # assets/ 下的文件名带内容哈希，可以长期缓存
            immutable = path.startswith("assets/")
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                if immutable else {"Cache-Control": "no-cache"})
        # 入口页不缓存：前端更新后刷新即可拿到新版本
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


_mount_frontend()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
