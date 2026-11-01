"""课程容器：只管理视频归属，不触及视频产物或问答。"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/courses", tags=["courses"])
COURSE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class CourseBody(BaseModel):
    title: str


def _title(raw: str) -> str:
    title = raw.strip()
    if not title or len(title) > 120:
        raise HTTPException(400, "课程名称须为 1–120 个字符")
    return title


def _course_id(course_id: str) -> str:
    if not COURSE_ID_RE.fullmatch(course_id):
        raise HTTPException(404, "课程不存在")
    return course_id


@router.get("")
def list_courses():
    return db.fetch_all("SELECT id, title, created_at FROM courses ORDER BY created_at DESC, rowid DESC")


@router.post("")
def create_course(body: CourseBody):
    course_id = uuid.uuid4().hex
    title = _title(body.title)
    created_at = db.now_iso()
    db.execute("INSERT INTO courses (id, title, created_at) VALUES (?, ?, ?)",
               (course_id, title, created_at))
    return {"id": course_id, "title": title, "created_at": created_at}


@router.patch("/{course_id}")
def rename_course(course_id: str, body: CourseBody):
    course_id = _course_id(course_id)
    title = _title(body.title)
    with db.connect() as conn:
        result = conn.execute("UPDATE courses SET title = ? WHERE id = ?", (title, course_id))
        if not result.rowcount:
            raise HTTPException(404, "课程不存在")
        course = dict(conn.execute("SELECT id, title, created_at FROM courses WHERE id = ?",
                                   (course_id,)).fetchone())
    return course


@router.delete("/{course_id}")
def delete_course(course_id: str):
    course_id = _course_id(course_id)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM courses WHERE id = ?", (course_id,)).fetchone():
            raise HTTPException(404, "课程不存在")
        if conn.execute("SELECT 1 FROM videos WHERE course_id = ? LIMIT 1", (course_id,)).fetchone():
            raise HTTPException(409, "课程中还有视频，请先移出")
        conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    return {"ok": True}
