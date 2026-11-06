"""SQLite storage layer for BBbot (subscriptions, seen article links, digests)."""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bbbot.db")

_LOCK = threading.Lock()
_conn_obj = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    chat_id   INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    title     TEXT,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    url       TEXT NOT NULL,
    title     TEXT,
    UNIQUE (chat_id, thread_id, url)
);

CREATE TABLE IF NOT EXISTS seen_links (
    sub_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    link   TEXT NOT NULL,
    PRIMARY KEY (sub_id, link)
);

CREATE TABLE IF NOT EXISTS raffles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    image_file_id TEXT,
    mode        TEXT NOT NULL DEFAULT 'join',   -- 'all' = auto-enroll, 'join' = button only
    announcement_message_id INTEGER,
    ends_at     TEXT NOT NULL,                  -- ISO-8601, UTC
    status      TEXT NOT NULL DEFAULT 'running',-- running / finished / cancelled
    winner_user_id INTEGER,
    winner_name TEXT
);

CREATE TABLE IF NOT EXISTS pending_articles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    link      TEXT NOT NULL,
    title     TEXT,
    added_at  TEXT NOT NULL,
    UNIQUE (chat_id, thread_id, link)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    raffle_id    INTEGER NOT NULL REFERENCES raffles(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    display_name TEXT,
    UNIQUE (raffle_id, user_id)
);
"""


def _conn():
    global _conn_obj
    if _conn_obj is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn_obj = conn
    return _conn_obj


def _run(sql, params=(), *, one=False, all_rows=False):
    with _LOCK:
        cur = _conn().execute(sql, params)
        result = None
        if one:
            result = cur.fetchone()
        elif all_rows:
            result = cur.fetchall()
        _conn().commit()
    return result if (one or all_rows) else cur


def init_db():
    with _LOCK:
        _conn().executescript(SCHEMA)
        _conn().commit()


# ---------------------------------------------------------------- topics ---

def upsert_topic(chat_id, thread_id, title):
    _run(
        "INSERT INTO topics (chat_id, thread_id, title) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id, thread_id) DO UPDATE SET title = excluded.title",
        (chat_id, thread_id, title),
    )


def topics_for_chat(chat_id):
    return _run(
        "SELECT thread_id, title FROM topics WHERE chat_id = ?", (chat_id,), all_rows=True
    ) or []


# ----------------------------------------------------------- subscriptions ---

def add_subscription(chat_id, thread_id, url, title=None):
    """Insert a subscription and return its id (existing id if already present)."""
    with _LOCK:
        cur = _conn().execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, thread_id, url, title) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, thread_id, url, title),
        )
        _conn().commit()
        if cur.rowcount:
            return cur.lastrowid
        row = _conn().execute(
            "SELECT id FROM subscriptions WHERE chat_id = ? AND thread_id = ? AND url = ?",
            (chat_id, thread_id, url),
        ).fetchone()
    return row["id"] if row else None


def update_subscription_title(sub_id, title):
    _run("UPDATE subscriptions SET title = ? WHERE id = ?", (title, sub_id))


def subscriptions_for_topic(chat_id, thread_id):
    return _run(
        "SELECT * FROM subscriptions WHERE chat_id = ? AND thread_id = ? ORDER BY id",
        (chat_id, thread_id), all_rows=True,
    ) or []


def subscriptions_for_chat(chat_id):
    return _run(
        "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY thread_id, id",
        (chat_id,), all_rows=True,
    ) or []


def all_subscriptions():
    return _run("SELECT * FROM subscriptions ORDER BY id", all_rows=True) or []


def delete_subscription(chat_id, sub_id):
    cur = _run("DELETE FROM subscriptions WHERE chat_id = ? AND id = ?", (chat_id, sub_id))
    return cur.rowcount > 0


# ------------------------------------------------------------- seen links ---

def seen_links(sub_id):
    rows = _run(
        "SELECT link FROM seen_links WHERE sub_id = ? LIMIT 1000", (sub_id,), all_rows=True
    ) or []
    return {r["link"] for r in rows}


def add_seen_link(sub_id, link):
    with _LOCK:
        _conn().execute(
            "INSERT OR IGNORE INTO seen_links (sub_id, link) VALUES (?, ?)", (sub_id, link)
        )
        # keep at most the latest 500 links per feed
        _conn().execute(
            "DELETE FROM seen_links WHERE sub_id = ? AND link NOT IN "
            "(SELECT link FROM seen_links WHERE sub_id = ? ORDER BY rowid DESC LIMIT 500)",
            (sub_id, sub_id),
        )
        _conn().commit()


# ---------------------------------------------------- pending articles -----

PENDING_CAP = 40   # max articles kept per topic between digests


def add_pending(chat_id, thread_id, link, title):
    """Queue one article for the next daily digest of this topic (newest kept)."""
    with _LOCK:
        _conn().execute(
            "INSERT OR IGNORE INTO pending_articles "
            "(chat_id, thread_id, link, title, added_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, link, title, datetime.now(timezone.utc).isoformat()),
        )
        _conn().execute(
            "DELETE FROM pending_articles WHERE chat_id = ? AND thread_id = ? "
            "AND id NOT IN (SELECT id FROM pending_articles "
            "WHERE chat_id = ? AND thread_id = ? ORDER BY id DESC LIMIT ?)",
            (chat_id, thread_id, chat_id, thread_id, PENDING_CAP),
        )
        _conn().commit()


def pending_topics():
    return _run(
        "SELECT DISTINCT chat_id, thread_id FROM pending_articles "
        "ORDER BY chat_id, thread_id",
        all_rows=True,
    ) or []


def get_pending(chat_id, thread_id):
    return _run(
        "SELECT link, title, added_at FROM pending_articles "
        "WHERE chat_id = ? AND thread_id = ? ORDER BY id",
        (chat_id, thread_id), all_rows=True,
    ) or []


def clear_pending(chat_id, thread_id):
    """Delete this topic's queued links — used right after the digest is posted."""
    _run(
        "DELETE FROM pending_articles WHERE chat_id = ? AND thread_id = ?",
        (chat_id, thread_id),
    )


# --------------------------------------------------------------- settings ---

def get_setting(key, default=None):
    row = _run("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    _run(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- raffles ---

def create_raffle(chat_id, thread_id, name, description, image_file_id, mode, ends_at):
    cur = _run(
        "INSERT INTO raffles "
        "(chat_id, thread_id, name, description, image_file_id, mode, ends_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, thread_id, name, description, image_file_id, mode, ends_at),
    )
    return cur.lastrowid


def set_announcement_message(raffle_id, message_id):
    _run(
        "UPDATE raffles SET announcement_message_id = ? WHERE id = ?",
        (message_id, raffle_id),
    )


def all_raffle_chats():
    rows = _run("SELECT DISTINCT chat_id FROM raffles", all_rows=True) or []
    return rows


def get_raffle(raffle_id):
    return _run("SELECT * FROM raffles WHERE id = ?", (raffle_id,), one=True)


def running_raffles(chat_id=None, thread_id=None, mode=None):
    sql = "SELECT * FROM raffles WHERE status = 'running'"
    params = []
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    if thread_id is not None:
        sql += " AND thread_id = ?"
        params.append(thread_id)
    if mode is not None:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY id"
    return _run(sql, tuple(params), all_rows=True) or []


def finish_raffle(raffle_id, status, winner_user_id=None, winner_name=None):
    _run(
        "UPDATE raffles SET status = ?, winner_user_id = ?, winner_name = ? WHERE id = ?",
        (status, winner_user_id, winner_name, raffle_id),
    )


def add_participant(raffle_id, user_id, display_name):
    """Returns True if the participant was newly added."""
    cur = _run(
        "INSERT OR IGNORE INTO participants (raffle_id, user_id, display_name) "
        "VALUES (?, ?, ?)",
        (raffle_id, user_id, display_name),
    )
    return cur.rowcount > 0


def participants(raffle_id):
    return _run(
        "SELECT * FROM participants WHERE raffle_id = ? ORDER BY rowid", (raffle_id,),
        all_rows=True,
    ) or []
