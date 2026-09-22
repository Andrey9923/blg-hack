from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

from model.operations import replay_episode
from web.app import OperatorServer, OperatorService
from web.persistence import Database, SQLiteRunStore


class PersistentApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'operator.sqlite3'
        self.database = Database(self.path)
        self.servers = []
        self.port = self.start_server()

    def start_server(self, auth=False):
        server = OperatorServer(('127.0.0.1', 0), OperatorService(SQLiteRunStore(Database(self.path))),
                                auth_database=self.database if auth else None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server.server_address[1]

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.directory.cleanup()

    def request(self, method, path, payload=None, token=None, port=None):
        connection = HTTPConnection('127.0.0.1', port or self.port, timeout=20)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def create(self, **kwargs):
        status, run = self.request('POST', '/api/runs', {'scenario_id':'P01_intro'}, **kwargs)
        self.assertEqual(status, 201, run)
        return run

    def test_restart_events_goal_and_replay(self):
        run = self.create()
        path = '/api/runs/' + run['run_id']
        self.assertEqual(self.request('POST', path+'/advance', {'steps':2})[0], 200)
        self.assertEqual(self.request('POST', path+'/goal', {'goal':'revenue'})[0], 200)
        self.assertEqual(self.request('POST', path+'/events', {
            'id':'boundary', 'at_step':2, 'type':'satellite_outage',
            'satellite_ids':['S01'], 'end_step':4})[0], 200)
        before = self.request('GET', path+'/result')[1]
        self.servers[0][0].shutdown()
        self.servers[0][0].server_close()
        self.servers[0][1].join(2)
        self.servers.clear()
        self.port = self.start_server()
        after = self.request('GET', path+'/result')[1]
        self.assertEqual(before, after)
        self.assertEqual(self.request('GET', '/api/runs')[1]['runs'][0]['run_id'], run['run_id'])
        self.assertEqual(self.request('POST', path+'/advance', {'steps':1})[0], 200)
        result = self.request('GET', path+'/result')[1]
        replay = replay_episode(result['initial_scenario'], result['events'], result['commands'], 3)
        self.assertEqual(replay.summary(), result['summary'])
        self.assertEqual(replay.env.trace, result['trace'])

    def test_more_than_32_runs(self):
        for _ in range(34):
            self.create()
        self.assertEqual(len(self.request('GET','/api/runs')[1]['runs']), 34)

    def test_select_branch_and_reject_stale_parent(self):
        parent = self.create()
        path = '/api/runs/' + parent['run_id']
        status, comparison = self.request('POST', path+'/compare', {'until_step':2})
        self.assertEqual(status, 200, comparison)
        branch = comparison['branches'][1]
        status, selected = self.request('POST', path+'/adopt', {'branch_id':branch['run_id']})
        self.assertEqual(status, 200, selected)
        self.assertEqual(selected['run_id'], parent['run_id'])
        self.assertEqual(selected['summary'], branch['summary'])
        self.assertEqual(selected['observation'], branch['observation'])
        self.assertEqual(self.request('GET', '/api/runs/'+branch['run_id'])[1]['observation'], branch['observation'])
        status, _ = self.request('POST', path+'/adopt', {'branch_id':comparison['branches'][0]['run_id']})
        self.assertEqual(status, 409)
        other = self.create()
        self.assertEqual(self.request('POST', '/api/runs/'+other['run_id']+'/adopt', {'branch_id':branch['run_id']})[0],400)
        exported = self.request('GET', path+'/result')[1]
        self.assertEqual(exported['trace'], self.request('GET','/api/runs/'+branch['run_id']+'/result')[1]['trace'])

    def test_goal_change_invalidates_branch_and_revision(self):
        parent = self.create(); path = '/api/runs/'+parent['run_id']
        branch = self.request('POST',path+'/fork', {})[1]
        self.request('POST',path+'/goal', {'goal':'revenue'})
        self.assertEqual(self.request('POST',path+'/adopt', {'branch_id':branch['run_id']})[0],409)
        self.assertEqual(self.request('POST',path+'/advance', {'steps':1, 'expected_revision':parent['revision']})[0],409)
        self.assertEqual(self.request('GET',path)[1]['observation']['step'],0)

    def test_invalid_event_rollback_and_concurrent_writers(self):
        run = self.create(); path = '/api/runs/'+run['run_id']
        before = self.request('GET',path+'/result')[1]
        self.assertEqual(self.request('POST',path+'/events', {'id':'bad'})[0],400)
        self.assertEqual(before,self.request('GET',path+'/result')[1])
        second_port = self.start_server()
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(self.request,'POST',path+'/advance',{'steps':1},port=port)
                       for port in (self.port, second_port)]
        self.assertEqual([f.result()[0] for f in futures],[200,200])
        self.assertEqual(self.request('GET',path)[1]['observation']['step'],2)

    def test_auth_isolation_roles_logout_and_password_reset(self):
        self.database.set_user('alice','alice-password-123')
        self.database.set_user('bob','bob-password-12345')
        port = self.start_server(auth=True)
        def login(name, password):
            status, body = self.request('POST','/api/auth/login',{'username':name,'password':password},port=port)
            self.assertEqual(status,200,body)
            return body['token']
        alice = login('alice','alice-password-123'); bob = login('bob','bob-password-12345')
        self.assertEqual(self.request('GET','/api/runs',port=port)[0],401)
        run = self.create(token=alice,port=port); path='/api/runs/'+run['run_id']
        for suffix in ('','/result','/explain?step=0&satellite_id=S01'):
            self.assertEqual(self.request('GET',path+suffix,token=bob,port=port)[0],404)
        self.assertEqual(self.request('POST',path+'/advance',{'steps':1},token=bob,port=port)[0],404)
        self.assertEqual(self.request('GET','/api/runs',token=bob,port=port)[1]['runs'],[])
        self.database.set_user('alice','alice-new-password','viewer')
        self.assertEqual(self.request('GET',path,token=alice,port=port)[0],401)
        alice=login('alice','alice-new-password')
        self.assertEqual(self.request('GET',path,token=alice,port=port)[0],200)
        self.assertEqual(self.request('POST',path+'/advance',{'steps':1},token=alice,port=port)[0],403)
        self.assertEqual(self.request('POST','/api/auth/logout',{},token=alice,port=port)[0],200)
        self.assertEqual(self.request('GET',path,token=alice,port=port)[0],401)
        self.assertEqual(self.request('POST','/api/auth/login',{'username':'bob','password':'wrong'},port=port)[0],401)

    def test_failed_commit_does_not_send_success(self):
        # A failed write must roll back the snapshot and return an error, not an ACK.
        run = self.create(); path='/api/runs/'+run['run_id']
        with self.database.session() as db:
            db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        self.assertEqual(self.request('POST',path+'/advance',{'steps':1})[0],500)
        self.assertEqual(self.request('GET',path)[1]['observation']['step'],0)
