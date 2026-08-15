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
                    bubble_enabled INTEGER NOT NULL DEFAULT 1,
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
                    last_bubble_at TEXT,
                    bubble_count INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS learning_skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    include_questions INTEGER NOT NULL DEFAULT 1,
                    include_mistakes INTEGER NOT NULL DEFAULT 1,
                    include_conversations INTEGER NOT NULL DEFAULT 1,
                    include_growth INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 0,
                    dirty INTEGER NOT NULL DEFAULT 1,
                    last_exported_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS learning_skill_sources (
                    skill_id INTEGER NOT NULL REFERENCES learning_skills(id) ON DELETE CASCADE,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    PRIMARY KEY(skill_id, source_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
                    user_message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                    assistant_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                    question TEXT NOT NULL,
                    conclusion TEXT NOT NULL DEFAULT '',
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS learning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                    question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL,
                    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                    value INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity_buckets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_date TEXT NOT NULL,
                    bucket_start TEXT NOT NULL,
                    application TEXT NOT NULL,
                    key_press_count INTEGER NOT NULL DEFAULT 0,
                    mouse_active_seconds INTEGER NOT NULL DEFAULT 0,
                    foreground_seconds INTEGER NOT NULL DEFAULT 0,
                    mouse_click_count INTEGER NOT NULL DEFAULT 0,
                    first_activity_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    UNIQUE(activity_date, bucket_start, application)
                );

                CREATE TABLE IF NOT EXISTS rag_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '',
                    knowledge_id TEXT NOT NULL DEFAULT '',
                    upload_path TEXT NOT NULL DEFAULT '/documents',
                    retrieval_path TEXT NOT NULL DEFAULT '/retrieval',
                    delete_path TEXT NOT NULL DEFAULT '/documents/{document_id}',
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rag_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id INTEGER NOT NULL REFERENCES rag_connections(id) ON DELETE RESTRICT,
                    local_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    remote_document_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'uploading',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    synced_at TEXT,
                    UNIQUE(connection_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    step_type TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, position);
                CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_id);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_attempts_question ON question_attempts(question_id, id);
                CREATE INDEX IF NOT EXISTS idx_unanswered_question ON unanswered_questions(question_id, id);
                CREATE INDEX IF NOT EXISTS idx_skill_sources_source ON learning_skill_sources(source_id, skill_id);
                CREATE INDEX IF NOT EXISTS idx_insights_source ON conversation_insights(source_id, id);
                CREATE INDEX IF NOT EXISTS idx_learning_events_source ON learning_events(source_id, id);
                CREATE INDEX IF NOT EXISTS idx_activity_buckets_date
                    ON activity_buckets(activity_date, bucket_start);
                CREATE INDEX IF NOT EXISTS idx_rag_documents_connection
                    ON rag_documents(connection_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation
                    ON agent_runs(conversation_id, created_at);
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
            conversation_additions = {
                "mode": "TEXT NOT NULL DEFAULT 'legacy'",
                "rag_connection_id": "INTEGER REFERENCES rag_connections(id) ON DELETE SET NULL",
                "work_dir": "TEXT NOT NULL DEFAULT ''",
                "memory_json": "TEXT NOT NULL DEFAULT '{}'",
                "status": "TEXT NOT NULL DEFAULT 'active'",
            }
            for column, definition in conversation_additions.items():
                if column not in conversation_columns:
                    self.conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source_id, updated_at)"
            )
            activity_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(activity_buckets)").fetchall()
            }
            if "foreground_seconds" not in activity_columns:
                self.conn.execute(
                    "ALTER TABLE activity_buckets ADD COLUMN foreground_seconds INTEGER NOT NULL DEFAULT 0"
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
            if "bubble_enabled" not in source_columns:
                self.conn.execute(
                    "ALTER TABLE sources ADD COLUMN bubble_enabled INTEGER NOT NULL DEFAULT 1"
                )
            skill_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(learning_skills)").fetchall()
            }
            if "dirty" not in skill_columns:
                self.conn.execute(
                    "ALTER TABLE learning_skills ADD COLUMN dirty INTEGER NOT NULL DEFAULT 1"
                )
            question_columns = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(questions)").fetchall()
            }
            if "last_bubble_at" not in question_columns:
                self.conn.execute("ALTER TABLE questions ADD COLUMN last_bubble_at TEXT")
            if "bubble_count" not in question_columns:
                self.conn.execute(
                    "ALTER TABLE questions ADD COLUMN bubble_count INTEGER NOT NULL DEFAULT 0"
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
        knowledge_type: str = "code",
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

    def set_source_bubble_enabled(self, source_id: int, enabled: bool) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE sources SET bubble_enabled = ? WHERE id = ?",
                (1 if enabled else 0, source_id),
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

    def list_code_sources(self) -> list[dict]:
        return [item for item in self.list_sources() if item.get("knowledge_type") == "code"]

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
            skill_ids = [
                int(row["skill_id"])
                for row in self.conn.execute(
                    "SELECT skill_id FROM learning_skill_sources WHERE source_id = ?", (source_id,)
                )
            ]
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
            if skill_ids:
                placeholders = ",".join("?" for _ in skill_ids)
                self.conn.execute(
                    f"UPDATE learning_skills SET updated_at = ?, dirty = 1 WHERE id IN ({placeholders})",
                    (utc_now(), *skill_ids),
                )

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
            self._touch_skills_for_source_locked(source_id)
        return ids

    @staticmethod
    def _question(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["options"] = json.loads(value.pop("options_json"))
        problem_title = value.get("problem_title", "").strip()
        if (
            value.get("knowledge_type") == "code"
            and problem_title
            and problem_title not in value["prompt"]
        ):
            value["prompt"] = f"在《{problem_title}》这道题中，{value['prompt']}"
        return value

    def get_question(self, question_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT q.*, s.name AS source_name, s.problem_title, s.problem_statement, s.knowledge_type FROM questions q "
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
        conditions = ["s.knowledge_type = 'code'"]
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
                "SELECT q.*, s.name AS source_name, s.problem_title, s.problem_statement, s.knowledge_type FROM questions q "
                f"JOIN sources s ON s.id = q.source_id {where} ORDER BY q.id",
                parameters,
            ).fetchall()
        return [self._question(row) for row in rows]

    def next_question(
        self,
        exclude_id: int | None = None,
        exclude_ids: list[int] | tuple[int, ...] | None = None,
        exclude_source_ids: list[int] | tuple[int, ...] | None = None,
        source_id: int | None = None,
        wrong_only: bool = False,
        unanswered_only: bool = False,
        bubble_only: bool = False,
        randomize: bool = False,
    ) -> dict | None:
        conditions = ["s.knowledge_type = 'code'"]
        parameters: list[object] = []
        if exclude_id is not None:
            conditions.append("q.id != ?")
            parameters.append(exclude_id)
        if exclude_ids:
            recent_question_ids = [int(item) for item in exclude_ids]
            placeholders = ",".join("?" for _ in recent_question_ids)
            conditions.append(f"q.id NOT IN ({placeholders})")
            parameters.extend(recent_question_ids)
        if exclude_source_ids:
            recent_source_ids = [int(item) for item in exclude_source_ids]
            placeholders = ",".join("?" for _ in recent_source_ids)
            conditions.append(f"q.source_id NOT IN ({placeholders})")
            parameters.extend(recent_source_ids)
        if source_id is not None:
            conditions.append("q.source_id = ?")
            parameters.append(source_id)
        if bubble_only:
            conditions.append("s.bubble_enabled = 1")
        if wrong_only:
            conditions.append("q.ask_count > q.correct_count")
        if unanswered_only:
            conditions.append(
                "EXISTS (SELECT 1 FROM unanswered_questions u "
                "WHERE u.question_id = q.id AND u.resolved_at IS NULL)"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            if bubble_only:
                # Bubble review is driven by learning difficulty and recency, rather
                # than source/question insertion order. A short cooldown prevents a
                # single question from immediately reappearing; the fallback keeps
                # small knowledge bases usable when every question is cooling down.
                bubble_where = f"{where} AND (q.last_bubble_at IS NULL OR q.last_bubble_at <= datetime('now', '-600 seconds'))"
                bubble_sql = f"""
                    SELECT q.*, s.name AS source_name, s.problem_title, s.problem_statement, s.knowledge_type,
                           COALESCE((SELECT SUM(CASE WHEN a.is_correct = 0 THEN 1 ELSE 0 END)
                                     FROM question_attempts a WHERE a.question_id = q.id), 0) AS wrong_count,
                           COALESCE((SELECT SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END)
                                     FROM question_attempts a WHERE a.question_id = q.id), 0) AS answered_correct_count,
                           (SELECT a.is_correct FROM question_attempts a
                            WHERE a.question_id = q.id ORDER BY a.id DESC LIMIT 1) AS last_attempt_correct,
                           CASE WHEN EXISTS (SELECT 1 FROM unanswered_questions u
                                             WHERE u.question_id = q.id AND u.resolved_at IS NULL)
                                THEN 1 ELSE 0 END AS has_unanswered
                    FROM questions q JOIN sources s ON s.id = q.source_id
                    {bubble_where}
                    ORDER BY
                        CASE
                            WHEN has_unanswered = 1 THEN 0
                            WHEN wrong_count > 0 AND answered_correct_count = 0 THEN 1
                            WHEN last_attempt_correct = 0 THEN 2
                            WHEN q.last_bubble_at IS NULL THEN 3
                            ELSE 4
                        END,
                        wrong_count DESC,
                        CASE WHEN q.last_bubble_at IS NULL THEN 0 ELSE 1 END,
                        COALESCE(q.last_bubble_at, '1970-01-01T00:00:00+00:00') ASC,
                        RANDOM()
                    LIMIT 1
                """
                row = self.conn.execute(bubble_sql, parameters).fetchone()
                if row is None:
                    row = self.conn.execute(
                        f"""
                        SELECT q.*, s.name AS source_name, s.problem_title, s.problem_statement, s.knowledge_type,
                               COALESCE((SELECT SUM(CASE WHEN a.is_correct = 0 THEN 1 ELSE 0 END)
                                         FROM question_attempts a WHERE a.question_id = q.id), 0) AS wrong_count,
                               COALESCE((SELECT SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END)
                                         FROM question_attempts a WHERE a.question_id = q.id), 0) AS answered_correct_count,
                               (SELECT a.is_correct FROM question_attempts a WHERE a.question_id = q.id
                                ORDER BY a.id DESC LIMIT 1) AS last_attempt_correct,
                               CASE WHEN EXISTS (SELECT 1 FROM unanswered_questions u
                                                 WHERE u.question_id = q.id AND u.resolved_at IS NULL)
                                    THEN 1 ELSE 0 END AS has_unanswered
                        FROM questions q JOIN sources s ON s.id = q.source_id
                        {where}
                        ORDER BY CASE WHEN has_unanswered = 1 THEN 0
                                      WHEN wrong_count > 0 AND answered_correct_count = 0 THEN 1
                                      WHEN last_attempt_correct = 0 THEN 2
                                      WHEN q.last_bubble_at IS NULL THEN 3 ELSE 4 END,
                                 wrong_count DESC,
                                 COALESCE(q.last_bubble_at, '1970-01-01T00:00:00+00:00') ASC,
                                 RANDOM()
                        LIMIT 1
                        """,
                        parameters,
                    ).fetchone()
            else:
                final_order = "RANDOM()" if randomize else "COALESCE(q.last_asked_at, q.created_at) ASC"
                row = self.conn.execute(
                    f"""
                    SELECT q.*, s.name AS source_name, s.problem_title, s.problem_statement, s.knowledge_type FROM questions q
                    JOIN sources s ON s.id = q.source_id
                    {where}
                    ORDER BY CASE WHEN q.last_asked_at IS NULL THEN 0 ELSE 1 END,
                             (q.correct_count * 1.0 / MAX(q.ask_count, 1)) ASC,
                              {final_order}
                    LIMIT 1
                    """,
                    parameters,
                ).fetchone()
        return self._question(row)

    def mark_question_bubbled(self, question_id: int) -> None:
        """Record that a question was actually selected for desktop display."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE questions SET last_bubble_at = ?, bubble_count = bubble_count + 1 WHERE id = ?",
                (utc_now(), question_id),
            )

    def answer_question(self, question_id: int, selected_index: int) -> dict:
        question = self.get_question(question_id)
        if not question:
            raise KeyError(f"Question {question_id} does not exist")
        correct = int(selected_index) == int(question["correct_index"])
        now = utc_now()
        with self._lock, self.conn:
            previous = self.conn.execute(
                "SELECT is_correct FROM question_attempts WHERE question_id = ? ORDER BY id DESC LIMIT 1",
                (question_id,),
            ).fetchone()
            had_wrong = self.conn.execute(
                "SELECT 1 FROM question_attempts WHERE question_id = ? AND is_correct = 0 LIMIT 1",
                (question_id,),
            ).fetchone() is not None
            self.conn.execute(
                "UPDATE questions SET ask_count = ask_count + 1, "
                "correct_count = correct_count + ?, last_asked_at = ? WHERE id = ?",
                (1 if correct else 0, now, question_id),
            )
            attempt = self.conn.execute(
                "INSERT INTO question_attempts(question_id, selected_index, is_correct, created_at) "
                "VALUES(?, ?, ?, ?)",
                (question_id, int(selected_index), 1 if correct else 0, now),
            )
            self.conn.execute(
                "UPDATE unanswered_questions SET resolved_at = ? "
                "WHERE question_id = ? AND resolved_at IS NULL",
                (now, question_id),
            )
            event_type = "question_correct" if correct else "question_wrong"
            self.conn.execute(
                "INSERT OR IGNORE INTO learning_events("
                "event_type, event_key, source_id, question_id, value, metadata_json, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    event_type,
                    f"attempt:{attempt.lastrowid}",
                    question["source_id"],
                    question_id,
                    1,
                    json.dumps({"selected_index": int(selected_index)}, ensure_ascii=False),
                    now,
                ),
            )
            if correct and had_wrong and previous is not None and not bool(previous["is_correct"]):
                self.conn.execute(
                    "INSERT OR IGNORE INTO learning_events("
                    "event_type, event_key, source_id, question_id, value, metadata_json, created_at"
                    ") VALUES('mistake_recovered', ?, ?, ?, 1, '{}', ?)",
                    (f"recovery:{question_id}:{attempt.lastrowid}", question["source_id"], question_id, now),
                )
            self._touch_skills_for_source_locked(int(question["source_id"]), now)
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

    def create_conversation(
        self,
        title: str,
        provider: str,
        source_id: int | None = None,
        *,
        mode: str = "chat",
        rag_connection_id: int | None = None,
        work_dir: str = "",
    ) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO conversations(title, provider, source_id, mode, rag_connection_id, work_dir, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (title[:80] or "新会话", provider, source_id, mode, rag_connection_id, work_dir, now, now),
            )
            return int(cursor.lastrowid)

    def list_conversations(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.*, s.name AS source_name, r.name AS rag_connection_name FROM conversations c "
                "LEFT JOIN sources s ON s.id = c.source_id "
                "LEFT JOIN rag_connections r ON r.id = c.rag_connection_id "
                "ORDER BY c.updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_rag_connection(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        knowledge_id: str = "",
        upload_path: str = "/documents",
        retrieval_path: str = "/retrieval",
        delete_path: str = "/documents/{document_id}",
        connection_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self._lock, self.conn:
            if connection_id:
                self.conn.execute(
                    "UPDATE rag_connections SET name=?, base_url=?, api_key=?, knowledge_id=?, "
                    "upload_path=?, retrieval_path=?, delete_path=?, updated_at=? WHERE id=?",
                    (name, base_url.rstrip('/'), api_key, knowledge_id, upload_path, retrieval_path,
                     delete_path, now, connection_id),
                )
                return int(connection_id)
            cursor = self.conn.execute(
                "INSERT INTO rag_connections(name,base_url,api_key,knowledge_id,upload_path,retrieval_path,delete_path,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (name, base_url.rstrip('/'), api_key, knowledge_id, upload_path, retrieval_path,
                 delete_path, now, now),
            )
            return int(cursor.lastrowid)

    def list_rag_connections(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT *, (SELECT COUNT(*) FROM rag_documents d WHERE d.connection_id=r.id) AS document_count "
                "FROM rag_connections r ORDER BY active DESC, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_rag_connection(self, connection_id: int | None = None) -> dict | None:
        with self._lock:
            if connection_id is None:
                row = self.conn.execute(
                    "SELECT * FROM rag_connections WHERE active=1 ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            else:
                row = self.conn.execute("SELECT * FROM rag_connections WHERE id=?", (connection_id,)).fetchone()
        return dict(row) if row else None

    def activate_rag_connection(self, connection_id: int) -> None:
        with self._lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM rag_connections WHERE id=?", (connection_id,)).fetchone():
                raise KeyError("RAG 连接不存在")
            self.conn.execute("UPDATE rag_connections SET active=0")
            self.conn.execute("UPDATE rag_connections SET active=1, updated_at=? WHERE id=?", (utc_now(), connection_id))

    def delete_rag_connection(self, connection_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM rag_connections WHERE id=?", (connection_id,))

    def add_rag_document(self, connection_id: int, path: str | Path, content_hash: str) -> tuple[int, bool]:
        resolved = Path(path).resolve()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM rag_documents WHERE connection_id=? AND content_hash=?",
                (connection_id, content_hash),
            ).fetchone()
            if row:
                return int(row["id"]), False
            cursor = self.conn.execute(
                "INSERT INTO rag_documents(connection_id,local_path,name,content_hash,created_at) VALUES(?,?,?,?,?)",
                (connection_id, str(resolved), resolved.name, content_hash, utc_now()),
            )
            return int(cursor.lastrowid), True

    def set_rag_document_status(self, document_id: int, status: str, remote_id: str = "", error: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE rag_documents SET status=?, remote_document_id=?, error=?, synced_at=? WHERE id=?",
                (status, remote_id, error, utc_now() if status == "ready" else None, document_id),
            )

    def get_rag_document(self, document_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rag_documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def list_rag_documents(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT d.*, r.name AS connection_name FROM rag_documents d "
                "JOIN rag_connections r ON r.id=d.connection_id ORDER BY d.created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_rag_document(self, document_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM rag_documents WHERE id=?", (document_id,))

    def create_agent_run(self, conversation_id: int, backend: str) -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO agent_runs(conversation_id,backend,created_at) VALUES(?,?,?)",
                (conversation_id, backend, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_agent_run(self, run_id: int, status: str = "completed", error: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE agent_runs SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, utc_now(), run_id),
            )

    def add_agent_step(self, run_id: int, step_type: str, name: str, input_value: dict, output: str, status: str = "completed") -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO agent_steps(run_id,step_type,name,input_json,output_text,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, step_type, name, json.dumps(input_value, ensure_ascii=False), output, status, utc_now()),
            )
            return int(cursor.lastrowid)

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
            source = self.conn.execute(
                "SELECT source_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if source and source["source_id"] is not None:
                self._touch_skills_for_source_locked(int(source["source_id"]))
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

    def _touch_skills_for_source_locked(self, source_id: int, timestamp: str | None = None) -> None:
        self.conn.execute(
            "UPDATE learning_skills SET updated_at = ?, dirty = 1 WHERE id IN ("
            "SELECT skill_id FROM learning_skill_sources WHERE source_id = ?)",
            (timestamp or utc_now(), source_id),
        )

    @staticmethod
    def _validate_skill_name(name: str) -> str:
        normalized = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized) or len(normalized) > 64:
            raise ValueError("Skill 标识只能包含小写字母、数字和连字符，且不能超过 64 个字符")
        return normalized

    def create_learning_skill(
        self,
        name: str,
        title: str,
        description: str,
        source_ids: list[int],
        *,
        include_questions: bool = True,
        include_mistakes: bool = True,
        include_conversations: bool = True,
        include_growth: bool = True,
    ) -> int:
        normalized = self._validate_skill_name(name)
        selected = sorted(set(int(value) for value in source_ids))
        if not selected:
            raise ValueError("至少选择一个知识来源")
        if not title.strip() or not description.strip():
            raise ValueError("请填写 Skill 名称和用途描述")
        now = utc_now()
        with self._lock, self.conn:
            existing = {
                int(row["id"])
                for row in self.conn.execute(
                    f"SELECT id FROM sources WHERE id IN ({','.join('?' for _ in selected)})",
                    selected,
                )
            }
            if existing != set(selected):
                raise ValueError("所选知识来源不存在")
            try:
                cursor = self.conn.execute(
                    "INSERT INTO learning_skills("
                    "name, title, description, include_questions, include_mistakes, "
                    "include_conversations, include_growth, created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized, title.strip()[:100], description.strip()[:1000],
                        int(include_questions), int(include_mistakes),
                        int(include_conversations), int(include_growth), now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Skill 标识 {normalized} 已存在") from exc
            skill_id = int(cursor.lastrowid)
            self.conn.executemany(
                "INSERT INTO learning_skill_sources(skill_id, source_id) VALUES(?, ?)",
                [(skill_id, source_id) for source_id in selected],
            )
        return skill_id

    def update_learning_skill(
        self,
        skill_id: int,
        name: str,
        title: str,
        description: str,
        source_ids: list[int],
        *,
        include_questions: bool = True,
        include_mistakes: bool = True,
        include_conversations: bool = True,
        include_growth: bool = True,
    ) -> None:
        normalized = self._validate_skill_name(name)
        selected = sorted(set(int(value) for value in source_ids))
        if not selected:
            raise ValueError("至少选择一个知识来源")
        if not title.strip() or not description.strip():
            raise ValueError("请填写 Skill 名称和用途描述")
        with self._lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM learning_skills WHERE id = ?", (skill_id,)).fetchone():
                raise ValueError("Skill 不存在")
            existing = {
                int(row["id"])
                for row in self.conn.execute(
                    f"SELECT id FROM sources WHERE id IN ({','.join('?' for _ in selected)})",
                    selected,
                )
            }
            if existing != set(selected):
                raise ValueError("所选知识来源不存在")
            try:
                self.conn.execute(
                "UPDATE learning_skills SET name = ?, title = ?, description = ?, "
                    "include_questions = ?, include_mistakes = ?, include_conversations = ?, "
                    "include_growth = ?, updated_at = ?, dirty = 1 WHERE id = ?",
                    (
                        normalized, title.strip()[:100], description.strip()[:1000],
                        int(include_questions), int(include_mistakes),
                        int(include_conversations), int(include_growth), utc_now(), skill_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Skill 标识 {normalized} 已存在") from exc
            self.conn.execute("DELETE FROM learning_skill_sources WHERE skill_id = ?", (skill_id,))
            self.conn.executemany(
                "INSERT INTO learning_skill_sources(skill_id, source_id) VALUES(?, ?)",
                [(skill_id, source_id) for source_id in selected],
            )

    def list_learning_skills(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT sk.*, COUNT(DISTINCT ss.source_id) AS source_count, "
                "COUNT(DISTINCT q.id) AS question_count "
                "FROM learning_skills sk "
                "LEFT JOIN learning_skill_sources ss ON ss.skill_id = sk.id "
                "LEFT JOIN questions q ON q.source_id = ss.source_id "
                "GROUP BY sk.id ORDER BY sk.updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_learning_skill(self, skill_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM learning_skills WHERE id = ?", (skill_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["source_ids"] = [
                int(item["source_id"])
                for item in self.conn.execute(
                    "SELECT source_id FROM learning_skill_sources WHERE skill_id = ? ORDER BY source_id",
                    (skill_id,),
                )
            ]
        return result

    def delete_learning_skill(self, skill_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM learning_skills WHERE id = ?", (skill_id,))

    def mark_learning_skill_exported(self, skill_id: int, version: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE learning_skills SET version = ?, last_exported_at = ?, updated_at = ?, dirty = 0 WHERE id = ?",
                (version, now, now, skill_id),
            )

    def add_conversation_insight(
        self,
        conversation_id: int,
        source_id: int,
        user_message_id: int,
        question: str,
    ) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO conversation_insights("
                "conversation_id, source_id, user_message_id, question, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (conversation_id, source_id, user_message_id, question.strip(), now, now),
            )
            self._touch_skills_for_source_locked(source_id, now)
        return int(cursor.lastrowid)

    def resolve_conversation_insight(
        self,
        user_message_id: int,
        assistant_message_id: int,
        conclusion: str,
        citations: list[dict],
    ) -> None:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT source_id FROM conversation_insights WHERE user_message_id = ?",
                (user_message_id,),
            ).fetchone()
            self.conn.execute(
                "UPDATE conversation_insights SET assistant_message_id = ?, conclusion = ?, "
                "citations_json = ?, resolved = 1, updated_at = ? WHERE user_message_id = ?",
                (
                    assistant_message_id, conclusion.strip(),
                    json.dumps(citations or [], ensure_ascii=False), now, user_message_id,
                ),
            )
            if row and row["source_id"] is not None:
                self._touch_skills_for_source_locked(int(row["source_id"]), now)

    def list_conversation_insights(self, source_ids: list[int]) -> list[dict]:
        selected = sorted(set(int(value) for value in source_ids))
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self._lock:
            rows = self.conn.execute(
                "SELECT i.*, c.title AS conversation_title, c.summary AS conversation_summary, "
                "s.name AS source_name "
                "FROM conversation_insights i "
                "JOIN conversations c ON c.id = i.conversation_id "
                "LEFT JOIN sources s ON s.id = i.source_id "
                f"WHERE i.source_id IN ({placeholders}) ORDER BY i.id",
                selected,
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["citations"] = json.loads(value.pop("citations_json"))
            result.append(value)
        return result

    def record_learning_event(
        self,
        event_type: str,
        event_key: str,
        *,
        source_id: int | None = None,
        question_id: int | None = None,
        conversation_id: int | None = None,
        value: int = 1,
        metadata: dict | None = None,
    ) -> bool:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO learning_events("
                "event_type, event_key, source_id, question_id, conversation_id, value, metadata_json, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_type, event_key, source_id, question_id, conversation_id, int(value),
                    json.dumps(metadata or {}, ensure_ascii=False), utc_now(),
                ),
            )
            if cursor.rowcount and source_id is not None:
                self._touch_skills_for_source_locked(source_id)
        return cursor.rowcount > 0

    def list_learning_events(self, source_ids: list[int]) -> list[dict]:
        selected = sorted(set(int(value) for value in source_ids))
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM learning_events WHERE source_id IN ({placeholders}) ORDER BY id",
                selected,
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["metadata"] = json.loads(value.pop("metadata_json"))
            result.append(value)
        return result

    def add_activity_buckets(self, rows: list[dict]) -> None:
        """Merge anonymous counters; raw keys, titles, text and coordinates are absent."""
        if not rows:
            return
        values = [
            (
                row["activity_date"],
                row["bucket_start"],
                row["application"],
                int(row.get("key_press_count", 0)),
                int(row.get("mouse_active_seconds", 0)),
                int(row.get("foreground_seconds", 0)),
                int(row.get("mouse_click_count", 0)),
                row["first_activity_at"],
                row["last_activity_at"],
            )
            for row in rows
        ]
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT INTO activity_buckets("
                "activity_date, bucket_start, application, key_press_count, "
                "mouse_active_seconds, foreground_seconds, mouse_click_count, first_activity_at, last_activity_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(activity_date, bucket_start, application) DO UPDATE SET "
                "key_press_count = key_press_count + excluded.key_press_count, "
                "mouse_active_seconds = mouse_active_seconds + excluded.mouse_active_seconds, "
                "foreground_seconds = foreground_seconds + excluded.foreground_seconds, "
                "mouse_click_count = mouse_click_count + excluded.mouse_click_count, "
                "first_activity_at = MIN(first_activity_at, excluded.first_activity_at), "
                "last_activity_at = MAX(last_activity_at, excluded.last_activity_at)",
                values,
            )

    def list_activity_buckets(self, activity_date: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT activity_date, bucket_start, application, key_press_count, "
                "mouse_active_seconds, foreground_seconds, mouse_click_count, first_activity_at, last_activity_at "
                "FROM activity_buckets WHERE activity_date = ? "
                "ORDER BY bucket_start, application",
                (activity_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_activity_summary(self, activity_date: str) -> list[dict]:
        """Structured application totals for the future reporting tool layer."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT application, SUM(key_press_count) AS key_press_count, "
                "SUM(mouse_active_seconds) AS mouse_active_seconds, "
                "SUM(foreground_seconds) AS foreground_seconds, "
                "SUM(mouse_click_count) AS mouse_click_count, "
                "MIN(first_activity_at) AS first_activity_at, "
                "MAX(last_activity_at) AS last_activity_at "
                "FROM activity_buckets WHERE activity_date = ? GROUP BY application "
                "ORDER BY key_press_count DESC, mouse_active_seconds DESC, application",
                (activity_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_activity_history(self) -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute("DELETE FROM activity_buckets")
        return cursor.rowcount
