"""课程归纳只改变视频所属容器；不改视频和会话数据。"""
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import artifacts, config, db
from app.main import app as _main_app  # 提前载入真实模块；旧 API 测试会临时替换阶段模块
from app.api import chat as chat_api, courses, videos
from app.qa import chat as qa_chat, conversations

VIDEO_ID = "abc123def456"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(courses.router)
    app.include_router(videos.router)
    app.include_router(chat_api.router)
    return TestClient(app)


def _video(video_id: str = VIDEO_ID) -> None:
    db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)",
               (video_id, "讲次 1", "lecture.mp4", db.now_iso(), "done"))


def test_old_database_videos_remain_unclassified(tmp_settings):
    path = config.settings.db_path
    path.unlink()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY,title TEXT NOT NULL,filename TEXT NOT NULL,duration_s REAL,created_at TEXT NOT NULL,status TEXT NOT NULL)")
        conn.execute("INSERT INTO videos VALUES (?,?,?,?,?,?)",
                     (VIDEO_ID, "旧视频", "old.mp4", 60, "2026-09-01T00:00:00Z", "done"))
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY,video_id TEXT,role TEXT,content TEXT,citations_json TEXT,created_at TEXT)")
        conn.execute("INSERT INTO chat_messages (video_id,role,content,created_at) VALUES (?,?,?,?)",
                     (VIDEO_ID, "user", "旧问题", "2026-09-01T00:00:00Z"))
    db.init_db()
    db.init_db()
    assert db.fetch_one("SELECT course_id FROM videos WHERE id=?", (VIDEO_ID,))["course_id"] is None
    assert db.fetch_one("SELECT content FROM chat_messages WHERE video_id=?", (VIDEO_ID,))["content"] == "旧问题"
    assert db.fetch_all("SELECT * FROM courses") == []
    assert _client().get(f"/api/videos/{VIDEO_ID}").json()["title"] == "旧视频"


def test_course_crud_and_video_membership_do_not_touch_video_or_chat(tmp_settings):
    _video()
    source = artifacts.video_dir(VIDEO_ID) / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"saved video")
    db.execute("INSERT INTO chat_messages (video_id,role,content,created_at) VALUES (?,?,?,?)",
               (VIDEO_ID, "user", "独立会话", db.now_iso()))
    with _client() as client:
        assert client.get("/api/videos").json()[0]["course_id"] is None
        created = client.post("/api/courses", json={"title": "  AI6103  "})
        assert created.status_code == 200
        first = created.json()["id"]
        assert created.json()["title"] == "AI6103"
        second = client.post("/api/courses", json={"title": "AI6102"}).json()["id"]
        assert len(client.get("/api/courses").json()) == 2
        assert client.patch(f"/api/courses/{first}", json={"title": "AI6103 课程"}).json()["title"] == "AI6103 课程"

        path = f"/api/videos/{VIDEO_ID}/course"
        assert client.put(path, json={"course_id": first}).json()["course_id"] == first
        assert client.get("/api/videos").json()[0]["course_id"] == first
        assert client.delete(f"/api/courses/{first}").status_code == 409
        assert client.put(path, json={"course_id": second}).json()["course_id"] == second
        assert client.delete(f"/api/courses/{first}").json() == {"ok": True}
        assert client.put(path, json={"course_id": None}).json()["course_id"] is None
        assert client.delete(f"/api/courses/{second}").json() == {"ok": True}

        assert client.put(path, json={"course_id": "missing"}).status_code == 404
        assert client.put("/api/videos/ffffffffffff/course", json={"course_id": None}).status_code == 404
        assert client.post("/api/courses", json={"title": "   "}).status_code == 400
        assert client.patch(f"/api/courses/{first}", json={"title": "x"}).status_code == 404
        assert client.delete(f"/api/courses/{first}").status_code == 404

    assert source.read_bytes() == b"saved video"
    assert db.fetch_one("SELECT content FROM chat_messages WHERE video_id=?", (VIDEO_ID,))["content"] == "独立会话"


def test_lectures_in_one_course_keep_independent_chat_sessions(tmp_settings):
    other = "def456abc123"
    _video()
    _video(other)
    with _client() as client:
        course_id = client.post("/api/courses", json={"title": "AI6103"}).json()["id"]
        for video_id in (VIDEO_ID, other):
            assert client.put(f"/api/videos/{video_id}/course", json={"course_id": course_id}).status_code == 200

        qa_chat._insert(VIDEO_ID, "user", "第一讲的问题", None, version=None)
        qa_chat._insert(other, "user", "第二讲的问题", None, version=None)
        first = client.get(f"/api/videos/{VIDEO_ID}/chat/conversations").json()
        second = client.get(f"/api/videos/{other}/chat/conversations").json()
        assert first["current_id"] != second["current_id"]
        assert first["conversations"][0]["title"] == "第一讲的问题"
        assert second["conversations"][0]["title"] == "第二讲的问题"

        assert client.post(f"/api/videos/{VIDEO_ID}/chat/conversations").status_code == 200
        assert client.get(f"/api/videos/{VIDEO_ID}/chat").json() == []
        assert [item["content"] for item in client.get(f"/api/videos/{other}/chat").json()] == ["第二讲的问题"]
        assert conversations.current(other) == second["current_id"]
