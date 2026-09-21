from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from web.app import OperatorServer, OperatorService


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = OperatorServer(("127.0.0.1", 0), OperatorService())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, payload=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def create(self):
        status, body = self.request("POST", "/api/runs", {"scenario_id": "P01_intro", "planner": "strategic"})
        self.assertEqual(status, 201)
        return body["run_id"]

    def test_health_and_scenarios(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in body["scenarios"]}, {"P01_intro", "P02_shift", "P03_energy", "P04_demand"})

    def test_create_get_advance(self) -> None:
        run_id = self.create()
        status, body = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["observation"]["step"], 0)
        self.assertIn("available_actions", body)
        status, body = self.request("POST", f"/api/runs/{run_id}/advance", {"until_step": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["observation"]["step"], 1)

    def test_invalid_event_is_atomic(self) -> None:
        run_id = self.create()
        self.request("POST", f"/api/runs/{run_id}/advance", {"steps": 1})
        before = self.request("GET", f"/api/runs/{run_id}")[1]
        status, body = self.request("POST", f"/api/runs/{run_id}/events", {
            "id": "bad", "at_step": 4, "type": "satellite_outage",
            "satellite_ids": ["S01"], "end_step": 5,
        })
        self.assertEqual(status, 400)
        self.assertIn("current unfinished step", body["message"])
        after = self.request("GET", f"/api/runs/{run_id}")[1]
        self.assertEqual(before["observation"], after["observation"])
        self.assertEqual(before["metadata"], after["metadata"])

    def test_explain_fork_isolation_and_result(self) -> None:
        run_id = self.create()
        self.request("POST", f"/api/runs/{run_id}/advance", {"until_step": 1})
        status, event_result = self.request("POST", f"/api/runs/{run_id}/events", {
            "id": "operator-outage", "at_step": 1, "type": "satellite_outage",
            "satellite_ids": ["S01"], "end_step": 3,
        })
        self.assertEqual(status, 200)
        self.assertTrue(event_result["event_accepted"])
        status, goal_result = self.request("POST", f"/api/runs/{run_id}/goal", {"goal": "revenue"})
        self.assertEqual(status, 200)
        self.assertEqual(goal_result["observation"]["goal"], "revenue")
        self.request("POST", f"/api/runs/{run_id}/advance", {"steps": 1})
        status, explanation = self.request("GET", f"/api/runs/{run_id}/explain?step=0&satellite_id=S01")
        self.assertEqual(status, 200)
        self.assertIn(explanation["reason"], ("idle", "accepted"))
        self.assertIn("energy", explanation)
        status, branch = self.request("POST", f"/api/runs/{run_id}/fork", {"branch_id": "test-branch", "goal": "revenue"})
        self.assertEqual(status, 201)
        branch_id = branch["run_id"]
        self.assertNotEqual(branch_id, run_id)
        self.request("POST", f"/api/runs/{branch_id}/advance", {"steps": 1})
        parent = self.request("GET", f"/api/runs/{run_id}")[1]
        child = self.request("GET", f"/api/runs/{branch_id}")[1]
        self.assertEqual(parent["observation"]["step"], 2)
        self.assertEqual(child["observation"]["step"], 3)
        status, result = self.request("GET", f"/api/runs/{branch_id}/result")
        self.assertEqual(status, 200)
        self.assertEqual(result["steps_executed"], 3)
        self.assertIn("trace", result)


if __name__ == "__main__":
    unittest.main()
