from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .models import Budget, RunState, TaskState, new_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    status        TEXT NOT NULL,
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    budget        TEXT NOT NULL,
    usage         TEXT NOT NULL,
    cluster       TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    deadline_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    title        TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    capability   TEXT NOT NULL,
    round        INTEGER NOT NULL DEFAULT 1,
    depends_on   TEXT NOT NULL,
    status       TEXT NOT NULL,
    assignee     TEXT,
    model_id     TEXT,
    model_name   TEXT,
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    steps        INTEGER NOT NULL DEFAULT 0,
    tokens       INTEGER NOT NULL DEFAULT 0,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, ord);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    role       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'chat',
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, ts);

CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    id        TEXT NOT NULL,
    run_id    TEXT NOT NULL,
    task_id   TEXT,
    type      TEXT NOT NULL,
    ts        REAL NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    provider_ids TEXT NOT NULL DEFAULT '[]',
    planner_id   TEXT NOT NULL DEFAULT '',
    is_default   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key     TEXT NOT NULL DEFAULT '',
    api_style   TEXT NOT NULL DEFAULT 'openai_chat',
    auth_style  TEXT NOT NULL DEFAULT 'bearer',
    extra_headers TEXT NOT NULL DEFAULT '{}',
    proxy       TEXT NOT NULL DEFAULT '',
    scores      TEXT NOT NULL DEFAULT '{}',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    timeout     INTEGER NOT NULL DEFAULT 0,
    stream      INTEGER NOT NULL DEFAULT 1,
    group_id    TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 0,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id            TEXT PRIMARY KEY,
    provider_id   TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model         TEXT NOT NULL,
    judge         TEXT NOT NULL,
    status        TEXT NOT NULL,
    average       REAL NOT NULL DEFAULT 0,
    scores        TEXT NOT NULL DEFAULT '{}',
    results       TEXT NOT NULL DEFAULT '{}',
    errors        TEXT NOT NULL DEFAULT '{}',
    usage         TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_provider
    ON evaluations(provider_id, created_at DESC);
"""


class Store:
    """SQLite persistence. The orchestrator is the only writer of run/task state."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_providers()
            self._backfill_messages()
            self._conn.commit()

    def _backfill_messages(self) -> None:
        """Give pre-conversation runs synthetic goal/result messages."""
        rows = self._conn.execute(
            "SELECT r.id, r.goal, r.result, r.created_at FROM runs r "
            "WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.run_id = r.id)"
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "INSERT INTO messages (id, run_id, role, kind, content, meta, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("msg"),
                    row["id"],
                    "user",
                    "goal",
                    row["goal"],
                    "{}",
                    row["created_at"],
                ),
            )
            if row["result"]:
                self._conn.execute(
                    "INSERT INTO messages (id, run_id, role, kind, content, meta, ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        new_id("msg"),
                        row["id"],
                        "assistant",
                        "result",
                        row["result"],
                        "{}",
                        row["created_at"] + 0.001,
                    ),
                )

    def _migrate_providers(self) -> None:
        """Add columns introduced after the first release."""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(providers)")
        }
        additions = (
            ("api_style", "TEXT NOT NULL DEFAULT 'openai_chat'"),
            ("auth_style", "TEXT NOT NULL DEFAULT 'bearer'"),
            ("extra_headers", "TEXT NOT NULL DEFAULT '{}'"),
            ("proxy", "TEXT NOT NULL DEFAULT ''"),
            ("scores", "TEXT NOT NULL DEFAULT '{}'"),
            ("reasoning_effort", "TEXT NOT NULL DEFAULT ''"),
            ("timeout", "INTEGER NOT NULL DEFAULT 0"),
            ("stream", "INTEGER NOT NULL DEFAULT 1"),
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("is_default", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, ddl in additions:
            if name not in columns:
                self._conn.execute(f"ALTER TABLE providers ADD COLUMN {name} {ddl}")

        # Old releases allowed exactly one "active" provider. Routing needs
        # several enabled providers, so replace that index with a default flag.
        self._conn.execute("DROP INDEX IF EXISTS idx_providers_active")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_default"
            " ON providers(is_default) WHERE is_default = 1"
        )

        task_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")
        }
        for name, ddl in (
            ("model_id", "TEXT"),
            ("model_name", "TEXT"),
            ("round", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in task_columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")

        run_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")
        }
        if "cluster" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN cluster TEXT NOT NULL DEFAULT '{}'"
            )

        # Providers created before groups existed become one-model groups.
        self._conn.execute("UPDATE providers SET group_id=id WHERE group_id=''")

        # Older databases have no default row: promote the first provider.
        row = self._conn.execute(
            "SELECT id FROM providers WHERE is_default=1 LIMIT 1"
        ).fetchone()
        if row is None:
            first = self._conn.execute(
                "SELECT id FROM providers ORDER BY is_active DESC, created_at LIMIT 1"
            ).fetchone()
            if first is not None:
                self._conn.execute(
                    "UPDATE providers SET is_active=1, is_default=1 WHERE id=?",
                    (first["id"],),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- runs ----------

    def create_run(self, run: RunState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, goal, status, result, error, budget, usage, cluster, created_at, updated_at, deadline_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.goal,
                    run.status,
                    run.result,
                    run.error,
                    run.budget.model_dump_json(),
                    self._usage_json(run),
                    json.dumps(run.cluster or {}, ensure_ascii=False),
                    run.created_at,
                    run.created_at,
                    run.deadline_at,
                ),
            )
            self._conn.commit()

    def update_run(self, run: RunState) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, result=?, error=?, usage=?, updated_at=? WHERE id=?",
                (
                    run.status,
                    run.result,
                    run.error,
                    self._usage_json(run),
                    time.time(),
                    run.id,
                ),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            tasks = self._conn.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY ord", (run_id,)
            ).fetchall()
        data = dict(row)
        data["budget"] = json.loads(data["budget"])
        data["usage"] = json.loads(data["usage"])
        data["cluster"] = json.loads(data.get("cluster") or "{}")
        data["tasks"] = [self._task_row(t) for t in tasks]
        data["progress"] = self._progress(data["tasks"])
        data["messages"] = self.list_messages(run_id)
        return data

    # ---------- conversation messages ----------

    def add_message(
        self,
        run_id: str,
        role: str,
        content: str,
        *,
        kind: str = "chat",
        meta: dict[str, Any] | None = None,
        message_id: str | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": message_id or new_id("msg"),
            "run_id": run_id,
            "role": role,
            "kind": kind,
            "content": content,
            "meta": meta or {},
            "ts": ts or time.time(),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, run_id, role, kind, content, meta, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    message["id"],
                    run_id,
                    role,
                    kind,
                    content,
                    json.dumps(message["meta"], ensure_ascii=False),
                    message["ts"],
                ),
            )
            self._conn.commit()
        return message

    def list_messages(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE run_id=? ORDER BY ts, rowid", (run_id,)
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        return self._message_row(row) if row else None

    def update_message(self, message_id: str, content: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET content=? WHERE id=?", (content, message_id)
            )
            self._conn.commit()
        return self.get_message(message_id)

    def delete_message(self, message_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE id=?", (message_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def update_run_goal(self, run_id: str, goal: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET goal=?, updated_at=? WHERE id=?",
                (goal, time.time(), run_id),
            )
            self._conn.commit()

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["meta"] = json.loads(data.get("meta") or "{}")
        except json.JSONDecodeError:
            data["meta"] = {}
        return data

    def load_run_state(self, run_id: str) -> RunState | None:
        data = self.get_run(run_id)
        if data is None:
            return None
        usage = data.get("usage") or {}
        run = RunState(
            id=data["id"],
            goal=data["goal"],
            status=data["status"],
            budget=Budget(**data["budget"]),
            result=data.get("result", ""),
            error=data.get("error", ""),
            steps=int(usage.get("steps") or 0),
            tokens=int(usage.get("tokens") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(usage.get("cost_usd") or 0),
            created_at=float(data.get("created_at") or time.time()),
            deadline_at=float(usage.get("deadline_at") or 0),
            cluster=data.get("cluster") or {},
        )
        for item in data.get("tasks") or []:
            run.tasks.append(
                TaskState(
                    id=item["id"],
                    run_id=item["run_id"],
                    order=int(item["ord"]),
                    title=item["title"],
                    instruction=item["instruction"],
                    capability=item["capability"],
                    round=int(item.get("round") or 1),
                    depends_on=list(item.get("depends_on") or []),
                    status=item["status"],
                    assignee=item.get("assignee"),
                    model_id=item.get("model_id"),
                    model_name=item.get("model_name"),
                    result=item.get("result", ""),
                    error=item.get("error", ""),
                    attempts=int(item.get("attempts") or 0),
                    steps=int(item.get("steps") or 0),
                    tokens=int(item.get("tokens") or 0),
                    started_at=item.get("started_at"),
                    finished_at=item.get("finished_at"),
                )
            )
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            counts = self._conn.execute(
                "SELECT run_id, status, COUNT(*) AS n FROM tasks GROUP BY run_id, status"
            ).fetchall()
        by_run: dict[str, dict[str, int]] = {}
        for c in counts:
            by_run.setdefault(c["run_id"], {})[c["status"]] = c["n"]
        out = []
        for row in rows:
            item = dict(row)
            item["budget"] = json.loads(item["budget"])
            item["usage"] = json.loads(item["usage"])
            item["cluster"] = json.loads(item.get("cluster") or "{}")
            item["task_counts"] = by_run.get(item["id"], {})
            out.append(item)
        return out

    # ---------- clusters ----------

    def _cluster_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["provider_ids"] = json.loads(item.get("provider_ids") or "[]")
        except json.JSONDecodeError:
            item["provider_ids"] = []
        item["is_default"] = bool(item.get("is_default"))
        return item

    def list_clusters(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM clusters ORDER BY created_at"
            ).fetchall()
        return [self._cluster_row(row) for row in rows]

    def get_cluster(self, cluster_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()
        return self._cluster_row(row) if row else None

    def default_cluster(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clusters WHERE is_default=1 LIMIT 1"
            ).fetchone()
        return self._cluster_row(row) if row else None

    def save_cluster(self, data: dict[str, Any]) -> dict[str, Any]:
        cluster_id = str(data.get("id") or "").strip() or new_id("cluster")
        now = time.time()
        provider_ids = [str(item) for item in (data.get("provider_ids") or [])]
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            if data.get("is_default"):
                self._conn.execute("UPDATE clusters SET is_default=0")
            self._conn.execute(
                "INSERT OR REPLACE INTO clusters"
                " (id, name, provider_ids, planner_id, is_default, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    cluster_id,
                    str(data.get("name") or "未命名集群")[:80],
                    json.dumps(provider_ids, ensure_ascii=False),
                    str(data.get("planner_id") or "")[:80],
                    1 if data.get("is_default") else 0,
                    created_at,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_cluster(cluster_id) or {}

    def delete_cluster(self, cluster_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def set_default_cluster(self, cluster_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("UPDATE clusters SET is_default=0")
            self._conn.execute(
                "UPDATE clusters SET is_default=1 WHERE id=?", (cluster_id,)
            )
            self._conn.commit()
        return True

    # ---------- tasks ----------

    def create_tasks(self, tasks: list[TaskState]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO tasks (id, run_id, ord, title, instruction, capability, round, depends_on, status,"
                " assignee, model_id, model_name, result, error, attempts, steps, tokens, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        t.id,
                        t.run_id,
                        t.order,
                        t.title,
                        t.instruction,
                        t.capability,
                        t.round,
                        json.dumps(t.depends_on, ensure_ascii=False),
                        t.status,
                        t.assignee,
                        t.model_id,
                        t.model_name,
                        t.result,
                        t.error,
                        t.attempts,
                        t.steps,
                        t.tokens,
                        t.started_at,
                        t.finished_at,
                    )
                    for t in tasks
                ],
            )
            self._conn.commit()

    def update_task(self, task: TaskState) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, assignee=?, model_id=?, model_name=?, result=?, error=?,"
                " attempts=?, steps=?, tokens=?, started_at=?, finished_at=? WHERE id=?",
                (
                    task.status,
                    task.assignee,
                    task.model_id,
                    task.model_name,
                    task.result,
                    task.error,
                    task.attempts,
                    task.steps,
                    task.tokens,
                    task.started_at,
                    task.finished_at,
                    task.id,
                ),
            )
            self._conn.commit()

    # ---------- events ----------

    def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (id, run_id, task_id, type, ts, payload) VALUES (?,?,?,?,?,?)",
                (
                    event["id"],
                    event["run_id"],
                    event.get("task_id"),
                    event["type"],
                    event["ts"],
                    json.dumps(event.get("payload", {}), ensure_ascii=False),
                ),
            )
            self._conn.commit()
            event = dict(event)
            event["seq"] = cur.lastrowid
            return event

    def get_events(self, run_id: str, after: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def last_event_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return int(row["seq"] or 0)

    # ---------- runtime settings ----------

    def get_app_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_app_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()

    def delete_app_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
            self._conn.commit()

    def all_app_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ---------- providers ----------

    def list_providers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM providers ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_active_provider(self) -> dict[str, Any] | None:
        """Return the default model (used by planner / fallback)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM providers WHERE is_default=1 AND is_active=1 LIMIT 1"
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM providers WHERE is_active=1 ORDER BY created_at LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def get_enabled_providers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM providers WHERE is_active=1 ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def count_providers(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM providers").fetchone()
        return int(row["n"])

    def upsert_provider(
        self,
        provider_id: str | None,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        api_style: str = "openai_chat",
        auth_style: str = "bearer",
        extra_headers: str = "",
        proxy: str = "",
        scores: str = "{}",
        reasoning_effort: str = "",
        timeout: int = 0,
        stream: bool = True,
        enabled: bool = True,
        is_default: bool = False,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        stamp = time.time()
        with self._lock:
            existing = None
            if provider_id:
                existing = self._conn.execute(
                    "SELECT * FROM providers WHERE id=?", (provider_id,)
                ).fetchone()

            if existing:
                fields = [
                    "name=?",
                    "base_url=?",
                    "model=?",
                    "api_style=?",
                    "auth_style=?",
                    "scores=?",
                    "reasoning_effort=?",
                    "timeout=?",
                    "stream=?",
                    "is_active=?",
                    "updated_at=?",
                ]
                params: list[Any] = [
                    name,
                    base_url,
                    model,
                    api_style,
                    auth_style,
                    scores,
                    reasoning_effort,
                    timeout,
                    1 if stream else 0,
                    1 if enabled else 0,
                    stamp,
                ]
                if api_key:
                    fields.append("api_key=?")
                    params.append(api_key)
                if extra_headers:
                    fields.append("extra_headers=?")
                    params.append(extra_headers)
                if proxy:
                    fields.append("proxy=?")
                    params.append(proxy)
                if group_id:
                    fields.append("group_id=?")
                    params.append(group_id)
                params.append(provider_id)
                self._conn.execute(
                    f"UPDATE providers SET {', '.join(fields)} WHERE id=?", params
                )
            else:
                provider_id = provider_id or new_id("prv")
                self._conn.execute(
                    "INSERT INTO providers (id, name, base_url, model, api_key, api_style,"
                    " auth_style, extra_headers, proxy, scores, reasoning_effort, timeout,"
                    " stream, group_id, is_active, is_default, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        provider_id,
                        name,
                        base_url,
                        model,
                        api_key,
                        api_style,
                        auth_style,
                        extra_headers or "{}",
                        proxy,
                        scores,
                        reasoning_effort,
                        timeout,
                        1 if stream else 0,
                        group_id or provider_id,
                        1 if enabled else 0,
                        stamp,
                        stamp,
                    ),
                )

            if is_default:
                self._conn.execute(
                    "UPDATE providers SET is_default=0 WHERE is_default=1"
                )
                self._conn.execute(
                    "UPDATE providers SET is_default=1, is_active=1 WHERE id=?",
                    (provider_id,),
                )
            else:
                default_row = self._conn.execute(
                    "SELECT id FROM providers WHERE is_default=1 LIMIT 1"
                ).fetchone()
                if default_row is None and enabled:
                    self._conn.execute(
                        "UPDATE providers SET is_default=1 WHERE id=?", (provider_id,)
                    )
                elif (
                    not enabled
                    and default_row is not None
                    and default_row["id"] == provider_id
                ):
                    self._conn.execute(
                        "UPDATE providers SET is_default=0 WHERE id=?", (provider_id,)
                    )
                    other = self._conn.execute(
                        "SELECT id FROM providers WHERE is_active=1 AND id<>?"
                        " ORDER BY created_at LIMIT 1",
                        (provider_id,),
                    ).fetchone()
                    if other is not None:
                        self._conn.execute(
                            "UPDATE providers SET is_default=1 WHERE id=?",
                            (other["id"],),
                        )
            self._conn.commit()
        return self.get_provider(provider_id) or {}

    def set_active_provider(self, provider_id: str) -> bool:
        return self.set_default_provider(provider_id)

    def set_default_provider(self, provider_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE providers SET is_default=0 WHERE is_default=1"
            )
            self._conn.execute(
                "UPDATE providers SET is_default=1, is_active=1 WHERE id=?",
                (provider_id,),
            )
            self._conn.commit()
        return True

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, is_default FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE providers SET is_active=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, time.time(), provider_id),
            )
            if not enabled and row["is_default"]:
                self._conn.execute(
                    "UPDATE providers SET is_default=0 WHERE id=?", (provider_id,)
                )
                other = self._conn.execute(
                    "SELECT id FROM providers WHERE is_active=1 AND id<>?"
                    " ORDER BY created_at LIMIT 1",
                    (provider_id,),
                ).fetchone()
                if other is not None:
                    self._conn.execute(
                        "UPDATE providers SET is_default=1 WHERE id=?", (other["id"],)
                    )
            elif enabled:
                default_row = self._conn.execute(
                    "SELECT id FROM providers WHERE is_default=1 LIMIT 1"
                ).fetchone()
                if default_row is None:
                    self._conn.execute(
                        "UPDATE providers SET is_default=1 WHERE id=?", (provider_id,)
                    )
            self._conn.commit()
        return True

    def delete_provider(self, provider_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT is_default FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
            cur = self._conn.execute("DELETE FROM providers WHERE id=?", (provider_id,))
            if row is not None and row["is_default"]:
                other = self._conn.execute(
                    "SELECT id FROM providers WHERE is_active=1 ORDER BY created_at LIMIT 1"
                ).fetchone()
                if other is None:
                    other = self._conn.execute(
                        "SELECT id FROM providers ORDER BY created_at LIMIT 1"
                    ).fetchone()
                if other is not None:
                    self._conn.execute(
                        "UPDATE providers SET is_default=1, is_active=1 WHERE id=?",
                        (other["id"],),
                    )
            self._conn.commit()
        return cur.rowcount > 0

    # ---------- evaluations ----------

    def save_evaluation(self, evaluation: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evaluations (id, provider_id, provider_name, model,"
                " judge, status, average, scores, results, errors, usage, created_at,"
                " finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evaluation["id"],
                    evaluation["provider_id"],
                    evaluation.get("provider_name", ""),
                    evaluation.get("model", ""),
                    evaluation.get("judge", ""),
                    evaluation.get("status", "running"),
                    float(evaluation.get("average") or 0),
                    json.dumps(evaluation.get("scores") or {}, ensure_ascii=False),
                    json.dumps(evaluation.get("results") or {}, ensure_ascii=False),
                    json.dumps(evaluation.get("errors") or {}, ensure_ascii=False),
                    json.dumps(evaluation.get("usage") or {}, ensure_ascii=False),
                    float(evaluation.get("created_at") or time.time()),
                    evaluation.get("finished_at"),
                ),
            )
            self._conn.commit()

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("scores", "results", "errors", "usage"):
            item[key] = json.loads(item[key] or "{}")
        return item

    def list_evaluations(
        self, provider_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock:
            if provider_id:
                rows = self._conn.execute(
                    "SELECT * FROM evaluations WHERE provider_id=?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (provider_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("scores", "results", "errors", "usage"):
                item[key] = json.loads(item[key] or "{}")
            out.append(item)
        return out

    def latest_evaluations(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for item in self.list_evaluations(limit=500):
            if item["provider_id"] not in latest:
                latest[item["provider_id"]] = item
        return latest

    # ---------- run deletion ----------

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._conn.execute("DELETE FROM tasks WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # ---------- helpers ----------

    @staticmethod
    def _usage_json(run: RunState) -> str:
        return json.dumps(
            {
                "steps": run.steps,
                "tokens": run.tokens,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "cost_usd": round(run.cost_usd, 6),
                "deadline_at": run.deadline_at,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["depends_on"] = json.loads(item["depends_on"])
        return item

    @staticmethod
    def _progress(tasks: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        counts["total"] = len(tasks)
        return counts
