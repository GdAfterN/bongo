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
                    knowledge_type TEXT NOT NULL DEFAULT 'document',
                    problem_title TEXT NOT NULL DEFAULT '',
                    problem_statement TEXT NOT NULL DEFAULT '',
                    solution_approach TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS question_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    selected_index INTEGER NOT NULL,
                    is_correct INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS unanswered_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, position);
                CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_id);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_attempts_question ON question_attempts(question_id, id);
                CREATE INDEX IF NOT EXISTS idx_unanswered_question ON unanswered_questions(question_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unanswered_open
                    ON unanswered_questions(question_id) WHERE resolved_at IS NULL;
                """
            )
            conversation_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "source_id" not in conversation_columns:
                self.conn.execute(
                    "ALTER TABLE conversations ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL"
                )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source_id, updated_at)"
            )
            source_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(sources)").fetchall()
            }
            added_knowledge_type = "knowledge_type" not in source_columns
            if added_knowledge_type:
                self.conn.execute(
                    "ALTER TABLE sources ADD COLUMN knowledge_type TEXT NOT NULL DEFAULT 'document'"
                )
            for column in ("problem_title", "problem_statement", "solution_approach"):
                if column not in source_columns:
                    self.conn.execute(
                        f"ALTER TABLE sources ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            if added_knowledge_type:
                code_kinds = (
                    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go",
                    ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift",
                )
                placeholders = ",".join("?" for _ in code_kinds)
                self.conn.execute(
                    f"UPDATE sources SET knowledge_type = 'code' WHERE kind IN ({placeholders})",
                    code_kinds,
                )
                rows = self.conn.execute(
                    "SELECT id, knowledge_type, content FROM sources"
                ).fetchall()
                for row in rows:
                    digest = hashlib.sha256(
                        f"{row['knowledge_type']}\0{row['content']}".encode("utf-8")
                    ).hexdigest()
                    self.conn.execute(
                        "UPDATE sources SET content_hash = ? WHERE id = ?",
                        (digest, row["id"]),
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

    def add_source(
        self,
        path: str | Path,
        content: str,
        knowledge_type: str = "document",
    ) -> tuple[int, bool]:
        if knowledge_type not in {"document", "code"}:
            raise ValueError("知识类型必须是 document 或 code")
        source_path = Path(path).resolve()
        digest = hashlib.sha256(f"{knowledge_type}\0{content}".encode("utf-8")).hexdigest()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM sources WHERE content_hash = ?", (digest,)
            ).fetchone()
            if row:
                return int(row["id"]), False
            cursor = self.conn.execute(
                "INSERT INTO sources(path, name, kind, content, content_hash, knowledge_type, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    str(source_path), source_path.name, source_path.suffix.lower(), content,
                    digest, knowledge_type, utc_now(),
                ),
            )
            return int(cursor.lastrowid), True

    def set_source_algorithm_metadata(
        self,
        source_id: int,
        problem_title: str,
        problem_statement: str,
        solution_approach: str,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET problem_title = ?, problem_statement = ?, "
                "solution_approach = ? WHERE id = ?",
                (problem_title, problem_statement, solution_approach, source_id),
            )

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

    def get_source(self, source_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT s.*, COUNT(DISTINCT c.id) AS chunk_count, "
                "COUNT(DISTINCT q.id) AS question_count FROM sources s "
                "LEFT JOIN chunks c ON c.source_id = s.id "
                "LEFT JOIN questions q ON q.source_id = s.id "
                "WHERE s.id = ? GROUP BY s.id",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

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

    def list_questions(
        self,
        source_id: int | None = None,
        wrong_only: bool = False,
        unanswered_only: bool = False,
    ) -> list[dict]:
        conditions = []
        parameters: list[object] = []
        if source_id is not None:
            conditions.append("q.source_id = ?")
            parameters.append(source_id)
        if wrong_only:
            conditions.append("q.ask_count > q.correct_count")
        if unanswered_only:
            conditions.append(
                "EXISTS (SELECT 1 FROM unanswered_questions u "
                "WHERE u.question_id = q.id AND u.resolved_at IS NULL)"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            rows = self.conn.execute(
                "SELECT q.*, s.name AS source_name FROM questions q "
                f"JOIN sources s ON s.id = q.source_id {where} ORDER BY q.id",
                parameters,
            ).fetchall()
        return [self._question(row) for row in rows]

    def next_question(
        self,
        exclude_id: int | None = None,
        source_id: int | None = None,
        wrong_only: bool = False,
        unanswered_only: bool = False,
    ) -> dict | None:
        conditions = []
        parameters: list[object] = []
        if exclude_id is not None:
            conditions.append("q.id != ?")
            parameters.append(exclude_id)
        if source_id is not None:
            conditions.append("q.source_id = ?")
            parameters.append(source_id)
        if wrong_only:
            conditions.append("q.ask_count > q.correct_count")
        if unanswered_only:
            conditions.append(
                "EXISTS (SELECT 1 FROM unanswered_questions u "
                "WHERE u.question_id = q.id AND u.resolved_at IS NULL)"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            row = self.conn.execute(
                f"""
                SELECT q.*, s.name AS source_name FROM questions q
                JOIN sources s ON s.id = q.source_id
                {where}
                ORDER BY CASE WHEN q.last_asked_at IS NULL THEN 0 ELSE 1 END,
                         (q.correct_count * 1.0 / MAX(q.ask_count, 1)) ASC,
                          COALESCE(q.last_asked_at, q.created_at) ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return self._question(row)

    def answer_question(self, question_id: int, selected_index: int) -> dict:
        question = self.get_question(question_id)
        if not question:
            raise KeyError(f"Question {question_id} does not exist")
        correct = int(selected_index) == int(question["correct_index"])
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE questions SET ask_count = ask_count + 1, "
                "correct_count = correct_count + ?, last_asked_at = ? WHERE id = ?",
                (1 if correct else 0, now, question_id),
            )
            self.conn.execute(
                "INSERT INTO question_attempts(question_id, selected_index, is_correct, created_at) "
                "VALUES(?, ?, ?, ?)",
                (question_id, int(selected_index), 1 if correct else 0, now),
            )
            self.conn.execute(
                "UPDATE unanswered_questions SET resolved_at = ? "
                "WHERE question_id = ? AND resolved_at IS NULL",
                (now, question_id),
            )
        return {"correct": correct, "question": question}

    def list_wrong_questions(self, source_id: int | None = None) -> list[dict]:
        return self.list_questions(source_id=source_id, wrong_only=True)

    def mark_question_unanswered(self, question_id: int) -> bool:
        if not self.get_question(question_id):
            raise KeyError(f"Question {question_id} does not exist")
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO unanswered_questions(question_id, created_at) VALUES(?, ?)",
                (question_id, utc_now()),
            )
        return cursor.rowcount > 0

    def list_unanswered_questions(self, source_id: int | None = None) -> list[dict]:
        return self.list_questions(source_id=source_id, unanswered_only=True)

    def list_attempts(self, question_id: int | None = None) -> list[dict]:
        with self._lock:
            if question_id is None:
                rows = self.conn.execute(
                    "SELECT a.*, q.source_id, q.prompt, q.topic, s.name AS source_name "
                    "FROM question_attempts a JOIN questions q ON q.id = a.question_id "
                    "JOIN sources s ON s.id = q.source_id ORDER BY a.id"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM question_attempts WHERE question_id = ? ORDER BY id",
                    (question_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def search_chunks(self, query: str, limit: int = 6, source_id: int | None = None) -> list[dict]:
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
                        WHERE chunks_fts MATCH ? AND (? IS NULL OR s.id = ?) ORDER BY score LIMIT ?
                        """,
                        (fts_query, source_id, source_id, limit),
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
            source_condition = " AND s.id = ?" if source_id is not None else ""
            if source_id is not None:
                parameters.append(source_id)
            parameters.append(limit)
            rows = self.conn.execute(
                "SELECT c.id, c.source_id, c.content, c.heading, s.name AS source_name, 0 AS score "
                "FROM chunks c JOIN sources s ON s.id = c.source_id "
                f"WHERE ({conditions}){source_condition} LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_conversation(self, title: str, provider: str, source_id: int | None = None) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO conversations(title, provider, source_id, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (title[:80] or "新对话", provider, source_id, now, now),
            )
            return int(cursor.lastrowid)

    def list_conversations(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.*, s.name AS source_name FROM conversations c "
                "LEFT JOIN sources s ON s.id = c.source_id "
                "ORDER BY c.updated_at DESC LIMIT ?", (limit,)
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

    def set_conversation_provider(self, conversation_id: int, provider: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE conversations SET provider = ?, updated_at = ? WHERE id = ?",
                (provider, utc_now(), conversation_id),
            )

    def get_conversation(self, conversation_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None
