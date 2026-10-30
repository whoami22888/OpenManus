"""Postgres-backed workspace store. No workspace data is stored on Render's ephemeral disk."""
from __future__ import annotations

import threading
import time

from manus_app.store import uid

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS manus_private;
CREATE TABLE IF NOT EXISTS manus_private.projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 pinned INTEGER NOT NULL DEFAULT 0, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS manus_private.agents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL,
 icon TEXT NOT NULL DEFAULT '✦', pinned INTEGER NOT NULL DEFAULT 0,
 created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS manus_private.tasks (
 id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
 project_id TEXT, agent_id TEXT, result TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS manus_tasks_updated ON manus_private.tasks(updated DESC);
CREATE TABLE IF NOT EXISTS manus_private.events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, task_id TEXT NOT NULL,
 kind TEXT NOT NULL, message TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS manus_events_task ON manus_private.events(task_id,id);
CREATE TABLE IF NOT EXISTS manus_private.files (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, size BIGINT NOT NULL,
 media_type TEXT NOT NULL, project_id TEXT, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS manus_private.automations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
 interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL, project_id TEXT,
 agent_id TEXT, next_run DOUBLE PRECISION NOT NULL, last_run DOUBLE PRECISION,
 created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS manus_automations_due
 ON manus_private.automations(enabled,next_run);
REVOKE ALL ON SCHEMA manus_private FROM PUBLIC;
"""

TABLES = frozenset(("projects", "agents", "tasks", "files", "automations"))


class PostgresStore:
    """Implements the existing SQLite Store interface with safe, scoped SQL.

    Use a Supabase session-pooler connection string (port 5432), never its
    published anon key. The private schema is not exposed through PostgREST.
    """
    def __init__(self, dsn: str):
        from psycopg import connect
        from psycopg.rows import dict_row
        self._connect = connect
        self._dsn = dsn
        self._row_factory = dict_row
        self._lock = threading.RLock()
        with self._db() as c:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)
            c.execute(
                "INSERT INTO manus_private.agents(id,name,instructions,icon,pinned,created) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                ("default", "Manus Agent",
                 "You are a capable autonomous general-purpose agent. Plan the task, work with available sandbox tools, verify your work, and report results.",
                 "✦", 1, time.time()),
            )

    def _db(self):
        return self._connect(self._dsn, row_factory=self._row_factory,
                             connect_timeout=10, prepare_threshold=None,
                             options="-c search_path=manus_private")

    @staticmethod
    def _table(table: str) -> str:
        if table not in TABLES:
            raise ValueError("Invalid table")
        return "manus_private." + table

    def all(self, table: str, order: str = "created DESC") -> list[dict]:
        if order not in ("created DESC", "updated DESC"):
            raise ValueError("Invalid order")
        with self._db() as db:
            return list(db.execute(f"SELECT * FROM {self._table(table)} ORDER BY {order}").fetchall())

    def one(self, table: str, id: str):
        with self._db() as db:
            return db.execute(f"SELECT * FROM {self._table(table)} WHERE id=%s",
                              (id,)).fetchone()

    def create_project(self, name: str, description: str = ""):
        id = uid("prj")
        with self._db() as db:
            db.execute("INSERT INTO manus_private.projects(id,name,description,created) VALUES(%s,%s,%s,%s)",
                       (id, name, description, time.time()))
        return self.one("projects", id)

    def create_agent(self, name: str, instructions: str, icon: str = "✦"):
        id = uid("agt")
        with self._db() as db:
            db.execute("INSERT INTO manus_private.agents(id,name,instructions,icon,created) VALUES(%s,%s,%s,%s,%s)",
                       (id, name, instructions, icon[:4], time.time()))
        return self.one("agents", id)

    def create_task(self, prompt: str, project_id=None, agent_id=None):
        id = uid("tsk")
        now = time.time()
        with self._db() as db:
            db.execute("INSERT INTO manus_private.tasks(id,prompt,status,project_id,agent_id,created,updated) "
                       "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                       (id, prompt, "queued", project_id, agent_id or "default", now, now))
        self.event(id, "created", "Task added to the queue")
        return self.one("tasks", id)

    def task_status(self, id: str, status: str, result: str = "", error: str = ""):
        if status not in ("queued", "running", "completed", "failed", "cancelled", "interrupted"):
            raise ValueError("Unknown status")
        with self._db() as db:
            row = db.execute("UPDATE manus_private.tasks SET status=%s,result=%s,error=%s,updated=%s "
                             "WHERE id=%s RETURNING id", (status, result, error, time.time(), id)).fetchone()
            if row is None:
                raise KeyError(id)
        self.event(id, "status", status if not error else f"{status}: {error[:250]}")
        return self.one("tasks", id)

    def event(self, id: str, kind: str, message: str):
        with self._db() as db:
            db.execute("INSERT INTO manus_private.events(task_id,kind,message,created) VALUES(%s,%s,%s,%s)",
                       (id, kind, message[:4000], time.time()))

    def events(self, id: str, last_id: int = 0):
        with self._db() as db:
            return list(db.execute("SELECT * FROM manus_private.events WHERE task_id=%s AND id>%s "
                                   "ORDER BY id LIMIT 200", (id, last_id)).fetchall())

    def pin(self, table: str, id: str, pinned: bool):
        if table not in ("tasks", "projects", "agents"):
            raise ValueError("Not pinnable")
        with self._db() as db:
            row = db.execute(f"UPDATE {self._table(table)} SET pinned=%s WHERE id=%s RETURNING id",
                             (int(pinned), id)).fetchone()
            if row is None:
                raise KeyError(id)
        return self.one(table, id)

    def add_file(self, name: str, size: int, media_type: str, project_id=None, id=None):
        id = id or uid("file")
        record = dict(id=id, name=name, size=size, media_type=media_type,
                      project_id=project_id, created=time.time())
        with self._db() as db:
            db.execute("INSERT INTO manus_private.files(id,name,size,media_type,project_id,created) "
                       "VALUES(%(id)s,%(name)s,%(size)s,%(media_type)s,%(project_id)s,%(created)s)", record)
        return record

    def automation(self, name: str, prompt: str, interval: int, project_id=None, agent_id=None):
        id = uid("aut")
        now = time.time()
        with self._db() as db:
            db.execute("INSERT INTO manus_private.automations("
                       "id,name,prompt,interval_minutes,enabled,project_id,agent_id,created,next_run)"
                       " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (id, name, prompt, interval, 1, project_id, agent_id or "default", now, now + interval*60))
        return self.one("automations", id)

    def toggle_automation(self, id: str, enabled: bool):
        with self._db() as db:
            row = db.execute("UPDATE manus_private.automations SET enabled=%s WHERE id=%s RETURNING id",
                             (int(enabled), id)).fetchone()
            if row is None:
                raise KeyError(id)
        return self.one("automations", id)

    def pop_due(self):
        now = time.time()
        with self._lock:
            with self._db() as db:
                rows = list(db.execute(
                    "SELECT * FROM manus_private.automations WHERE enabled=1 AND next_run<=%s "
                    "ORDER BY next_run FOR UPDATE SKIP LOCKED LIMIT 10", (now,)).fetchall())
                for item in rows:
                    db.execute("UPDATE manus_private.automations SET last_run=%s,next_run=%s "
                               "WHERE id=%s", (now, now+item["interval_minutes"]*60, item["id"]))
                return rows

    def recover(self):
        with self._db() as db:
            db.execute("UPDATE manus_private.tasks SET status='interrupted', "
                       "error='Agent server restarted; rerun task to retry.',updated=%s "
                       "WHERE status IN ('queued','running')", (time.time(),))

    def delete_file(self, id: str):
        with self._db() as db:
            db.execute("DELETE FROM manus_private.files WHERE id=%s", (id,))
