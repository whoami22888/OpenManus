"""SQLite-backed application state. Every read uses its own connection.

The database lives outside source code and needs a persistent volume in production.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def uid(prefix: str) -> str:
    return prefix + '_' + uuid.uuid4().hex


class Store:
    def __init__(self, database: str):
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL,
                    icon TEXT NOT NULL DEFAULT '✦', pinned INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    project_id TEXT, agent_id TEXT, result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id,id);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    media_type TEXT NOT NULL, project_id TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    project_id TEXT, agent_id TEXT, next_run REAL NOT NULL,
                    last_run REAL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS automations_due ON automations(enabled,next_run);
            ''')
            con.execute("INSERT OR IGNORE INTO agents(id,name,instructions,icon,pinned,created) VALUES(?,?,?,?,?,?)",(
                'default','Manus Agent','You are a capable autonomous general-purpose agent. Plan the task, work with available sandbox tools, verify your work, and report results.', '✦',1,time.time()))

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=20, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        return db

    def all(self, table: str, order: str = 'created DESC') -> list[dict]:
        assert table in {'projects','agents','tasks','files','automations'}
        assert order in {'created DESC','updated DESC'}
        with self._db() as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}')]

    def one(self, table: str, id: str) -> dict | None:
        assert table in {'projects','agents','tasks','files','automations'}
        with self._db() as db:
            row = db.execute(f'SELECT * FROM {table} WHERE id=?',(id,)).fetchone()
            return dict(row) if row else None

    def create_project(self, name: str, description: str='') -> dict:
        record = {'id':uid('prj'),'name':name,'description':description,'created':time.time()}
        with self._db() as db: db.execute('INSERT INTO projects(id,name,description,created) VALUES(:id,:name,:description,:created)',record)
        return self.one('projects',record['id'])

    def create_agent(self, name: str, instructions: str, icon: str='✦') -> dict:
        record = {'id':uid('agt'),'name':name,'instructions':instructions,'icon':icon[:4],'created':time.time()}
        with self._db() as db: db.execute('INSERT INTO agents(id,name,instructions,icon,created) VALUES(:id,:name,:instructions,:icon,:created)',record)
        return self.one('agents',record['id'])

    def create_task(self,prompt: str,project_id: str|None=None,agent_id: str|None=None) -> dict:
        now=time.time(); record={'id':uid('tsk'),'prompt':prompt,'status':'queued', 'project_id':project_id,
                                   'agent_id':agent_id or 'default', 'created':now,'updated':now}
        with self._db() as db: db.execute('''INSERT INTO tasks(id,prompt,status,project_id,agent_id,created,updated)
                           VALUES(:id,:prompt,:status,:project_id,:agent_id,:created,:updated)''',record)
        self.event(record['id'],'created','Task added to the queue')
        return self.one('tasks',record['id'])

    def task_status(self,id: str,status: str,result: str='',error: str='') -> dict:
        assert status in ('queued','running','completed','failed','cancelled','interrupted')
        with self._db() as db:
            cur=db.execute('UPDATE tasks SET status=?,result=?,error=?,updated=? WHERE id=?',(status,result,error,time.time(),id))
            if not cur.rowcount: raise KeyError(id)
        self.event(id,'status', status if not error else f'{status}: {error[:250]}')
        return self.one('tasks',id)

    def event(self,id: str,kind: str,message: str):
        with self._db() as db: db.execute('INSERT INTO events(task_id,kind,message,created) VALUES(?,?,?,?)',(id,kind,message[:4000],time.time()))

    def events(self,id: str,last_id: int=0) -> list[dict]:
        with self._db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 200',(id,last_id))]

    def pin(self,table: str,id: str,pinned: bool):
        assert table in {'tasks','projects','agents'}
        with self._db() as db:
            cur=db.execute(f'UPDATE {table} SET pinned=? WHERE id=?',(int(pinned),id))
            if not cur.rowcount: raise KeyError(id)
        return self.one(table,id)

    def add_file(self,name: str,size: int,media_type: str,project_id: str|None=None) -> dict:
        v={'id':uid('file'),'name':name,'size':size,'media_type':media_type,'project_id':project_id,'created':time.time()}
        with self._db() as db: db.execute('INSERT INTO files(id,name,size,media_type,project_id,created) VALUES(:id,:name,:size,:media_type,:project_id,:created)',v)
        return v

    def automation(self,name:str,prompt:str,interval:int,project_id:str|None=None,agent_id:str|None=None) -> dict:
        v={'id':uid('aut'),'name':name,'prompt':prompt,'interval_minutes':interval,'enabled':1,
           'project_id':project_id,'agent_id':agent_id or 'default','created':time.time(),'next_run':time.time()+interval*60}
        with self._db() as db: db.execute('''INSERT INTO automations(id,name,prompt,interval_minutes,enabled,project_id,agent_id,created,next_run)
                                             VALUES(:id,:name,:prompt,:interval_minutes,:enabled,:project_id,:agent_id,:created,:next_run)''',v)
        return self.one('automations',v['id'])

    def toggle_automation(self,id:str,enabled:bool):
        with self._db() as db:
            cur=db.execute('UPDATE automations SET enabled=? WHERE id=?',(int(enabled),id))
            if not cur.rowcount:raise KeyError(id)
        return self.one('automations',id)

    def pop_due(self) -> list[dict]:
        # One transaction atomically claims each due automation, even with concurrent schedulers.
        now=time.time()
        with self._lock:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                rows=[dict(r) for r in db.execute('SELECT * FROM automations WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT 10',(now,))]
                for r in rows:
                    db.execute('UPDATE automations SET last_run=?,next_run=? WHERE id=? AND next_run=?',
                               (now,now+r['interval_minutes']*60,r['id'],r['next_run']))
                return rows

    def recover(self):
        with self._db() as db:
            db.execute("UPDATE tasks SET status='interrupted', error='Agent server restarted; rerun task to retry.',updated=? WHERE status IN ('queued','running')",(time.time(),))
