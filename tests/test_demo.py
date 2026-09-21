from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_demo_result_replays_with_identical_summary_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "demo-result.json"
            replay_path = Path(directory) / "demo-replay.json"
            demo = subprocess.run(
                [sys.executable, "scripts/demo.py", "--output", str(result_path)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Manual event accepted", demo.stdout)
            self.assertIn("Replay verified: exact summary and trace", demo.stdout)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["schema_version"], "cosmo-B-ops-result-1.0")
            self.assertEqual(set(result["demo_report"]["branches"]), {"priority", "revenue"})
            self.assertEqual(
                result["demo_report"]["replay_verified"],
                {"priority": True, "revenue": True},
            )

            subprocess.run(
                [sys.executable, "model/operations.py", "--result", str(result_path),
                 "--output", str(replay_path)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            self.assertEqual(replay["summary"], result["summary"])
            self.assertEqual(replay["trace"], result["trace"])
            self.assertEqual(replay["events"], result["events"])
            self.assertEqual(replay["commands"], result["commands"])

    def test_demo_supports_p02(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/demo.py", "--scenario", "P02"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Scenario: P02_shift", completed.stdout)
        self.assertIn("Replay verified: exact summary and trace", completed.stdout)


if __name__ == "__main__":
    unittest.main()
