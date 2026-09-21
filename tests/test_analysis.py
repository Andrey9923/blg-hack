from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from model.operations import digest
from model.resource_env import load
from planner.analysis import analyze_scenario
from planner.runtime import PlannerRuntime


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ('P01_intro', 'P02_shift', 'P03_energy', 'P04_demand')


class AnalysisAndRegressionTests(unittest.TestCase):
    def test_all_scenarios_and_goals_replay_exactly(self) -> None:
        for scenario_id in SCENARIOS:
            scenario = load(ROOT / 'data' / f'{scenario_id}.json')
            for goal in ('priority', 'revenue'):
                with self.subTest(scenario=scenario_id, goal=goal):
                    runtime = PlannerRuntime(scenario, planner='strategic', goal=goal)
                    runtime.run()
                    summary = runtime.summary()
                    self.assertEqual(summary['blocked_command_count'], 0)
                    self.assertEqual(summary['brownout_satellite_steps'], 0)
                    self.assertEqual(summary['critical_soc_satellite_steps'], 0)
                    self.assertEqual(summary['below_reserve_satellite_steps'], sum(
                        row['below_reserve'] for row in runtime.session.env.trace
                    ))
                    if scenario_id == 'P03_energy':
                        self.assertGreater(summary['below_reserve_satellite_steps'], 0)
                    replayed = runtime.replay()
                    self.assertEqual(replayed.summary(), summary)
                    self.assertEqual(replayed.env.trace, runtime.session.env.trace)
                    self.assertEqual(digest(replayed.env.trace), digest(runtime.session.env.trace))

    def test_events_demo_is_boundary_driven_and_replayable_for_both_goals(self) -> None:
        scenario = load(ROOT / 'data' / 'P02_shift.json')
        events = json.loads(
            (ROOT / 'examples' / 'events_demo.json').read_text(encoding='utf-8')
        )['events']
        for goal in ('priority', 'revenue'):
            with self.subTest(goal=goal):
                runtime = PlannerRuntime(scenario, planner='strategic', goal=goal)
                for event in events:
                    runtime.run_until(event['at_step'])
                    runtime.apply_event(event)
                runtime.run()
                self.assertEqual(runtime.summary()['blocked_command_count'], 0)
                self.assertEqual(runtime.replay().summary(), runtime.summary())
                self.assertEqual(runtime.replay().env.trace, runtime.session.env.trace)

    def test_analysis_report_has_deltas_losses_and_resource_indicators(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        report = analyze_scenario(scenario, goal='revenue')
        self.assertEqual(report['schema_version'], 'cosmo-B-planner-analysis-1.0')
        self.assertIn('reason_counts', report['strategic'])
        self.assertIn('unfinished_jobs', report['strategic'])
        self.assertIn('resource_indicators', report['strategic'])
        self.assertIn('jobs_completed', report['delta'])
        self.assertEqual(report['strategic']['summary']['blocked_command_count'], 0)

    def test_analysis_cli_runs_from_repository_root(self) -> None:
        completed = subprocess.run(
            [sys.executable, '-m', 'planner.analysis',
             '--scenario', 'data/P01_intro.json', '--goal', 'priority'],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload['scenario_id'], 'P01_intro')
        self.assertIn('delta', payload)

    def test_high_load_stays_within_reasonable_runtime(self) -> None:
        scenario = load(ROOT / 'data' / 'P04_demand.json')
        started = time.perf_counter()
        runtime = PlannerRuntime(scenario, planner='strategic', goal='revenue')
        runtime.run()
        elapsed = time.perf_counter() - started
        self.assertEqual(runtime.summary()['jobs_total'], 8120)
        self.assertEqual(runtime.summary()['blocked_command_count'], 0)
        self.assertLess(elapsed, 15.0)


if __name__ == '__main__':
    unittest.main()
