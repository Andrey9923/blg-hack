from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from model.operations import replay_episode
from web.app import OperatorServer, OperatorService
from web.maintenance import maintain
from web.persistence import Database, SQLiteRunStore


class OperatorToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.db = Database(self.path / 'operator.sqlite3')
        self.server = OperatorServer(('127.0.0.1', 0), OperatorService(SQLiteRunStore(self.db)),
                                     cors_origin='https://frontend.example')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=30)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        finally:
            connection.close()

    def create(self):
        code, run, _ = self.request('POST', '/api/v1/runs', {'scenario_id': 'P01_intro'})
        self.assertEqual(code, 201)
        return run

    def test_versioned_auth_cors_and_errors(self):
        code, body, headers = self.request('GET', '/api/v1/auth/session', headers={'Origin': 'https://frontend.example'})
        self.assertEqual(code, 200)
        self.assertEqual(body['username'], 'local')
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://frontend.example')
        self.assertEqual(self.request('OPTIONS', '/api/v1/runs', headers={'Origin': 'https://bad.example'})[0], 403)
        self.assertEqual(self.request('OPTIONS', '/api/v1/runs', headers={'Origin': 'https://frontend.example'})[0], 204)
        code, body, _ = self.request('POST', '/api/v1/runs', {}, {'Content-Type': 'text/plain'})
        self.assertEqual((code, body['error']), (415, 'unsupported_media_type'))
        self.assertEqual(self.request('POST', '/api/v1/scenarios/prepare', {'scenario': {}})[1]['error'], 'invalid_scenario')

    def test_schedule_preview_restart_execution_and_replay(self):
        run = self.create()
        path = '/api/v1/runs/' + run['run_id']
        entries = [{'step': 0, 'satellite_id': 'S01', 'action': {'action': 'idle'}},
                   {'step': 1, 'satellite_id': 'S01', 'action': {'action': 'calibrate'}}]
        code, saved, _ = self.request('POST', path+'/schedule', {'entries': entries, 'expected_revision': run['revision']})
        self.assertEqual(code, 200)
        before = self.request('GET', path+'/result')[1]
        code, preview, _ = self.request('POST', path+'/preview', {'until_step': 3})
        self.assertEqual(code, 200)
        self.assertEqual(self.request('GET', path+'/result')[1], before)
        self.assertEqual(preview['telemetry']['S01'][0]['executed'], 'idle')
        self.assertEqual(preview['telemetry']['S01'][1]['executed'], 'calibrate')
        # Every HTTP request restores from SQLite, so edits already crossed a restore boundary.
        self.assertEqual(self.request('GET', path)[1]['metadata']['scheduled_actions'], saved['metadata']['scheduled_actions'])
        code, after, _ = self.request('POST', path+'/advance', {'steps': 3})
        self.assertEqual(code, 200)
        self.assertEqual(after['summary'], preview['summary'])
        result = self.request('GET', path+'/result')[1]
        replay = replay_episode(result['initial_scenario'], result['events'], result['commands'], 3)
        self.assertEqual(replay.summary(), result['summary'])
        self.assertEqual(replay.env.trace, result['trace'])
        code, error, _ = self.request('POST', path+'/schedule', {'entries': entries})
        self.assertEqual((code, error['error']), (400, 'invalid_schedule'))
        self.assertEqual(self.request('GET', path+'/result')[1], result)
        self.assertEqual(self.request('POST', path+'/schedule', {'entries': [], 'expected_revision': run['revision']})[0], 409)

    def test_rate_limit_and_versioned_auth_are_enforced(self):
        self.server.rate_limit = 2
        self.assertEqual(self.request('GET', '/api/v1/scenarios')[0], 200)
        self.assertEqual(self.request('GET', '/api/scenarios')[0], 200)
        code, body, headers = self.request('GET', '/api/v1/scenarios')
        self.assertEqual((code, body['error'], headers['Retry-After']), (429, 'rate_limited', '60'))
        self.assertEqual(self.request('GET', '/health')[0], 200)
        self.server.rate_limit = 0
        self.server.auth_database = self.db
        self.db.set_user('reader', 'reader-password-123', 'viewer')
        self.assertEqual(self.request('GET', '/api/v1/runs')[0], 401)
        code, login, _ = self.request('POST', '/api/v1/auth/login', {'username':'reader', 'password':'reader-password-123'})
        self.assertEqual(code, 200)
        headers = {'Authorization': 'Bearer '+login['token']}
        self.assertEqual(self.request('GET', '/api/v1/auth/session', headers=headers)[1]['role'], 'viewer')
        self.assertEqual(self.request('POST', '/api/v1/runs', {'scenario_id':'P01_intro'}, headers)[0], 403)

    def test_backup_and_retention_preserve_active_runs_and_ancestors(self):
        parent = self.create()
        path = '/api/v1/runs/' + parent['run_id']
        child = self.request('POST', path+'/fork', {})[1]
        self.request('POST', path+'/advance', {'until_step': 48})
        second = self.create()
        self.request('POST', '/api/v1/runs/'+second['run_id']+'/advance', {'until_step': 48})
        third = self.create()
        self.request('POST', '/api/v1/runs/'+third['run_id']+'/advance', {'until_step': 48})
        result = maintain(self.db, self.path/'backups', keep_backups=2, keep_runs=1)
        self.assertEqual(result['removed_runs'], 1)
        self.assertEqual(self.request('GET', '/api/v1/runs/'+second['run_id'])[0], 404)
        self.assertEqual(self.request('GET', path)[0], 200)  # ancestor of active child
        self.assertEqual(self.request('GET', '/api/v1/runs/'+child['run_id'])[0], 200)
        conn = sqlite3.connect(result['backup'])
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM runs').fetchone()[0], 4)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            conn.close()
        maintain(self.db, self.path/'backups', keep_backups=2)
        maintain(self.db, self.path/'backups', keep_backups=2)
        self.assertEqual(len(list((self.path/'backups').glob('*.sqlite3'))), 2)
        with self.assertRaises(ValueError):
            self.db.backup(self.db.path)


if __name__ == '__main__':
    unittest.main()
