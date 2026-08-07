from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StudyDatabase:
    """SQLite-backed source of truth for knowledge, practice and conversations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'processing',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
                    prompt TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    correct_index INTEGER NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    ask_count INTEGER NOT NULL DEFAULT 0,
                    correct_count INTEGER NOT NULL DEFAULT 0,
                    last_asked_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_session_id TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, position);
                CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_id);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
                """
            )
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                    "USING fts5(content, heading, source_name, chunk_id UNINDEXED, tokenize='unicode61')"
                )
            except sqlite3.OperationalError:
                pass

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def add_source(self, path: str | Path, content: str) -> tuple[int, bool]:
        source_path = Path(path).resolve()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM sources WHERE content_hash = ?", (digest,)
            ).fetchone()
            if row:
                return int(row["id"]), False
            cursor = self.conn.execute(
                "INSERT INTO sources(path, name, kind, content, content_hash, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (str(source_path), source_path.name, source_path.suffix.lower(), content, digest, utc_now()),
            )
            return int(cursor.lastrowid), True

    def set_source_status(self, source_id: int, status: str, error: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET status = ?, error = ? WHERE id = ?",
                (status, error, source_id),
            )

    def list_sources(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT s.*, COUNT(DISTINCT c.id) AS chunk_count,
                       COUNT(DISTINCT q.id) AS question_count
                FROM sources s
                LEFT JOIN chunks c ON c.source_id = s.id
                LEFT JOIN questions q ON q.source_id = s.id
                GROUP BY s.id ORDER BY s.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_source(self, source_id: int) -> None:
        with self._lock, self.conn:
            chunk_ids = [
                str(row["id"])
                for row in self.conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,))
            ]
            if chunk_ids:
                try:
                    self.conn.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({','.join('?' for _ in chunk_ids)})",
                        chunk_ids,
                    )
                except sqlite3.OperationalError:
                    pass
            self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    def replace_chunks(self, source_id: int, chunks: list[dict]) -> list[int]:
        ids: list[int] = []
        with self._lock, self.conn:
            source = self.conn.execute("SELECT name FROM sources WHERE id = ?", (source_id,)).fetchone()
            old_chunk_ids = [
                str(row["id"])
                for row in self.conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,))
            ]
            if old_chunk_ids:
                try:
                    self.conn.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({','.join('?' for _ in old_chunk_ids)})",
                        old_chunk_ids,
                    )
                except sqlite3.OperationalError:
                    pass
            self.conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            for position, chunk in enumerate(chunks):
                cursor = self.conn.execute(
                    "INSERT INTO chunks(source_id, position, heading, content) VALUES(?, ?, ?, ?)",
                    (source_id, position, chunk.get("heading", ""), chunk["content"]),
                )
                chunk_id = int(cursor.lastrowid)
                ids.append(chunk_id)
                try:
                    self.conn.execute(
                        "INSERT INTO chunks_fts(content, heading, source_name, chunk_id) VALUES(?, ?, ?, ?)",
                        (chunk["content"], chunk.get("heading", ""), source["name"], chunk_id),
                    )
                except sqlite3.OperationalError:
                    pass
        return ids

    def add_questions(self, source_id: int, questions: list[dict]) -> list[int]:
        ids: list[int] = []
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM questions WHERE source_id = ?", (source_id,))
            for question in questions:
                cursor = self.conn.execute(
                    """
                    INSERT INTO questions(
                        source_id, chunk_id, prompt, options_json, correct_index,
                        explanation, evidence, topic, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        question.get("chunk_id"),
                        question["question"],
                        json.dumps(question["options"], ensure_ascii=False),
                        int(question["correct_index"]),
                        question.get("explanation", ""),
                        question.get("evidence", ""),
                        question.get("topic", ""),
                        utc_now(),
                    ),
                )
                ids.append(int(cursor.lastrowid))
        return ids

    @staticmethod
    def _question(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["options"] = json.loads(value.pop("options_json"))
        return value

    def get_question(self, question_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT q.*, s.name AS source_name FROM questions q "
                "JOIN sources s ON s.id = q.source_id WHERE q.id = ?",
                (question_id,),
            ).fetchone()
        return self._question(row)

    def next_question(self) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT q.*, s.name AS source_name FROM questions q
                JOIN sources s ON s.id = q.source_id
                ORDER BY CASE WHEN q.last_asked_at IS NULL THEN 0 ELSE 1 END,
                         (q.correct_count * 1.0 / MAX(q.ask_count, 1)) ASC,
                         COALESCE(q.last_asked_at, q.created_at) ASC
                LIMIT 1
                """
            ).fetchone()
        return self._question(row)

    def answer_question(self, question_id: int, selected_index: int) -> dict:
        question = self.get_question(question_id)
        if not question:
            raise KeyError(f"Question {question_id} does not exist")
        correct = int(selected_index) == int(question["correct_index"])
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE questions SET ask_count = ask_count + 1, "
                "correct_count = correct_count + ?, last_asked_at = ? WHERE id = ?",
                (1 if correct else 0, utc_now(), question_id),
            )
        return {"correct": correct, "question": question}

    def search_chunks(self, query: str, limit: int = 6) -> list[dict]:
        tokens = re.findall(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query)
        fts_query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:12])
        with self._lock:
            if fts_query:
                try:
                    rows = self.conn.execute(
                        """
                        SELECT c.id, c.content, c.heading, s.name AS source_name,
                               bm25(chunks_fts) AS score
                        FROM chunks_fts
                        JOIN chunks c ON c.id = CAST(chunks_fts.chunk_id AS INTEGER)
                        JOIN sources s ON s.id = c.source_id
                        WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                    if rows:
                        return [dict(row) for row in rows]
                except sqlite3.OperationalError:
                    pass
            search_terms: list[str] = []
            for token in tokens:
                if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 2:
                    search_terms.extend(token[index : index + 2] for index in range(len(token) - 1))
                else:
                    search_terms.append(token)
            search_terms = list(dict.fromkeys(search_terms))[:12]
            if not search_terms:
                search_terms = [query.strip()[:80]] if query.strip() else []
            if not search_terms:
                return []
            conditions = " OR ".join("c.content LIKE ? OR c.heading LIKE ?" for _ in search_terms)
            parameters: list[object] = []
            for term in search_terms:
                parameters.extend((f"%{term}%", f"%{term}%"))
            parameters.append(limit)
            rows = self.conn.execute(
                "SELECT c.id, c.content, c.heading, s.name AS source_name, 0 AS score "
                "FROM chunks c JOIN sources s ON s.id = c.source_id "
                f"WHERE {conditions} LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_conversation(self, title: str, provider: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO conversations(title, provider, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (title[:80] or "新对话", provider, now, now),
            )
            return int(cursor.lastrowid)

    def list_conversations(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_message(self, conversation_id: int, role: str, content: str, citations=None) -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO messages(conversation_id, role, content, citations_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (conversation_id, role, content, json.dumps(citations or [], ensure_ascii=False), utc_now()),
            )
            self.conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (utc_now(), conversation_id)
            )
            return int(cursor.lastrowid)

    def get_messages(self, conversation_id: int, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (conversation_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["citations"] = json.loads(value.pop("citations_json"))
            result.append(value)
        return result

    def set_conversation_summary(self, conversation_id: int, summary: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE conversations SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, utc_now(), conversation_id),
            )

    def get_conversation(self, conversation_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None
