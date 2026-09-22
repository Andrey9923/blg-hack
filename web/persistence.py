"""SQLite snapshots; no executable object serialization."""
from __future__ import annotations

import copy
import json
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path

from model.operations import replay_episode
from planner.runtime import PlannerRuntime
from web.app import RunStore


class Database:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.path).exists():
            Path(self.path).touch(mode=0o600, exist_ok=True)
        with self.session() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError('Unsupported database schema version')
            db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA user_version=2;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                    updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner, updated);
                DROP TABLE IF EXISTS sessions;
                DROP TABLE IF EXISTS users;
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=60)

    @contextmanager
    def session(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

def restore(payload):
    metadata = payload['run_metadata']
    run = PlannerRuntime(payload['initial_scenario'], planner=metadata['algorithm'], goal=metadata['goal'])
    run.session = replay_episode(payload['initial_scenario'], payload['events'],
                                 payload['commands'], payload['steps_executed'])
    run.session.run_metadata = copy.deepcopy(metadata)
    run.history = copy.deepcopy(payload['history'])
    if run.state_digest() != payload['state_digest']:
        raise ValueError('Stored run failed integrity verification')
    return run


class SQLiteRunStore(RunStore):
    """One transaction per request. Fresh snapshots prevent lost cross-process updates.

    SQLite serializes writers. A request-local cache is discarded after commit or
    rollback; the number of stored runs is not tied to a resident-object limit.
    """
    def __init__(self, database):
        super().__init__()
        self.database = database
        self._db = None
        self._owner = 'local'

    @contextmanager
    def transaction(self, write=False):
        with self._lock:
            db = self.database.connect()
            self._db = db
            self._runs, self._locks = {}, {}
            try:
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield
                if write:
                    for run_id, run in self._runs.items():
                        db.execute('''INSERT INTO runs VALUES (?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,
                            payload=excluded.payload, updated=excluded.updated''',
                            (run_id, 'local', json.dumps(run.result(), ensure_ascii=False, allow_nan=False), time.time()))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                self._runs, self._locks = {}, {}
                self._db = None
                db.close()

    def get(self, run_id):
        if run_id not in self._runs:
            row = self._db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('run not found')
            run = restore(json.loads(row[0]))
            self._runs[run_id] = run
            self._locks[run_id] = threading.RLock()
        return super().get(run_id)

    def add_forks(self, branches):
        for branch in branches:
            if self._db.execute('SELECT 1 FROM runs WHERE id=?', (branch.run_metadata['run_id'],)).fetchone():
                raise ValueError('fork generated a duplicate run id')
        super().add_forks(branches)

    def list_runs(self):
        rows = self._db.execute('''SELECT id, json_extract(payload, '$.run_metadata.goal'),
            json_extract(payload, '$.steps_executed'), updated FROM runs
            ORDER BY updated DESC''').fetchall()
        return [{'run_id': row[0], 'goal': row[1], 'step': row[2], 'updated': row[3]} for row in rows]
