"""
Layer database sederhana pakai SQLite.
Ini adalah "database sementara" untuk KB & log transkrip sesuai PRD Bab 11,
dibuat agar mudah dipetakan ke skema CRM saat migrasi nanti.
"""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import settings

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kb_articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',       -- JSON array of keywords
                category TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS call_transcripts (
                id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL,
                speaker TEXT NOT NULL,        -- 'agent' | 'customer' | 'unknown'
                text TEXT NOT NULL,
                ts TEXT NOT NULL,
                kb_suggested_ids TEXT DEFAULT '[]'  -- JSON array of kb_articles.id
            );

            CREATE INDEX IF NOT EXISTS idx_transcripts_call_id
                ON call_transcripts(call_id);
            """
        )


# ---------- KB articles ----------

def kb_list():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM kb_articles ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def kb_get(article_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
        return dict(row) if row else None


def kb_create(title: str, content: str, tags=None, category: str = None) -> dict:
    article_id = str(uuid.uuid4())
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO kb_articles (id, title, content, tags, category, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (article_id, title, content, json.dumps(tags or []), category, _now()),
        )
    return kb_get(article_id)


def kb_update(article_id: str, **fields) -> dict | None:
    if not kb_get(article_id):
        return None
    allowed = {"title", "content", "tags", "category"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "tags":
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return kb_get(article_id)
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(article_id)
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE kb_articles SET {', '.join(sets)} WHERE id = ?", params)
    return kb_get(article_id)


def kb_delete(article_id: str) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM kb_articles WHERE id = ?", (article_id,))
        return cur.rowcount > 0


# ---------- Call transcripts ----------

def transcript_add(call_id: str, speaker: str, text: str, kb_suggested_ids=None) -> dict:
    # FIX: sebelumnya _lock ini dideklarasikan tapi TIDAK PERNAH dipakai.
    # Selama transcript_add() dipanggil satu-satu di event loop, itu masih
    # "aman" secara kebetulan. Tapi sekarang (lihat fix di pipeline.py)
    # fungsi ini dipanggil lewat run_in_executor, artinya beberapa
    # panggilan bisa jalan di THREAD BERBEDA secara bersamaan (mis. leg
    # agent & customer ngomong nyaris bersamaan, atau dua call beda
    # sedang berlangsung). Tanpa lock, tulis-menulis bersamaan ke file
    # SQLite yang sama bisa nabrak dan lempar
    # `sqlite3.OperationalError: database is locked`.
    row_id = str(uuid.uuid4())
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO call_transcripts (id, call_id, speaker, text, ts, kb_suggested_ids) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, call_id, speaker, text, _now(), json.dumps(kb_suggested_ids or [])),
        )
    return {
        "id": row_id,
        "call_id": call_id,
        "speaker": speaker,
        "text": text,
        "kb_suggested_ids": kb_suggested_ids or [],
    }


def transcript_list_for_call(call_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_transcripts WHERE call_id = ? ORDER BY ts ASC", (call_id,)
        ).fetchall()
        return [dict(r) for r in rows]
