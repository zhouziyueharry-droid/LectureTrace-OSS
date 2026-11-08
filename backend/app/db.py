"""SQLite 建表与访问。每次调用新建连接，线程安全。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  duration_s REAL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,           -- processing | awaiting_roi | awaiting_confirm | done | failed
  course_id TEXT                  -- NULL = 未归类
);
CREATE TABLE IF NOT EXISTS stages (
  video_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,           -- pending | running | done | failed | skipped
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (video_id, name)
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  role TEXT NOT NULL,             -- user | assistant
  content TEXT NOT NULL,
  citations_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_conversations (
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  custom_title TEXT
);
CREATE INDEX IF NOT EXISTS chat_conversations_video ON chat_conversations(video_id, created_at);
CREATE TABLE IF NOT EXISTS api_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT,
  stage TEXT,
  provider TEXT NOT NULL,
  model TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  images INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 建表之后追加的列：(表, 列, 类型)。旧数据库在 init_db 时补上，已存在就跳过。
ADDED_COLUMNS = (
    ("videos", "course_id", "TEXT"),
    # 实际处理档位（响应里的 service_tier：flex / default 等）；DeepL、Anthropic 与未返回档位的接口为 NULL
    ("api_usage", "service_tier", "TEXT"),
    # 问答（交接 0001 §10.2）：ok | failed（引用修正后仍无效）；旧消息默认 ok
    ("chat_messages", "status", "TEXT NOT NULL DEFAULT 'ok'"),
    # 回答时 evidence.json 的 created_at；与当前版本不一致的消息 citations 置空、stale=true。旧消息为 NULL
    ("chat_messages", "evidence_version", "TEXT"),
    ("chat_messages", "conversation_id", "TEXT"),
    ("chat_messages", "learning_options_json", "TEXT"),
    ("chat_conversations", "custom_title", "TEXT"),
)

CODEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  usage_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(CODEX_SCHEMA)
        for table, column, col_type in ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        # 旧库每视频只有一段对话；沿用原 Codex ID 以保留 notes 和读取日志。
        for row in conn.execute("SELECT id FROM videos UNION SELECT video_id FROM chat_messages").fetchall():
            video_id = row['id']
            if conn.execute("SELECT 1 FROM chat_conversations WHERE video_id = ?", (video_id,)).fetchone():
                continue
            old = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f'codex_conversation:{video_id}',)).fetchone()
            state = json.loads(old['value']) if old else {}
            cid = state.get('conversation_id') or uuid.uuid4().hex
            created = conn.execute("SELECT MIN(created_at) AS value FROM chat_messages WHERE video_id = ?", (video_id,)).fetchone()['value'] or now_iso()
            conn.execute("INSERT INTO chat_conversations (id,video_id,created_at,updated_at) VALUES (?,?,?,?)",
                         (cid, video_id, created, now_iso()))
            conn.execute("UPDATE chat_messages SET conversation_id = ? WHERE video_id = ? AND conversation_id IS NULL", (cid, video_id))
            conn.execute("INSERT OR REPLACE INTO app_settings VALUES (?,?)", (f'chat_current:{video_id}', json.dumps(cid)))


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        return conn.execute(sql, params).lastrowid


def record_usage(*, video_id: str | None, stage: str, provider: str, model: str | None,
                 input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                 images: int = 0, cost_usd: float = 0.0, service_tier: str | None = None) -> None:
    execute(
        "INSERT INTO api_usage (video_id, stage, provider, model, input_tokens, output_tokens,"
        " cache_read_tokens, images, cost_usd, service_tier, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (video_id, stage, provider, model, input_tokens, output_tokens, cache_read_tokens,
         images, cost_usd, service_tier, now_iso()),
    )


def get_setting(key: str, default: Any = None) -> Any:
    row = fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
