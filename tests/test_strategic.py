from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from model.operations import Session, replay_episode
from model.resource_env import load
from planner.baseline import run_baseline
from planner.compare import compare_scenario
from planner.strategic import StrategicPlanner, run_strategic

from test_baseline import P01, ROOT


def compact_scenario(jobs: list[dict], steps: int = 3) -> dict:
    scenario = load(P01)
    satellite = copy.deepcopy(scenario['satellites'][0])
    scenario['meta'] = {'id': 'synthetic-strategic', 'title': 'synthetic'}
    scenario['time']['steps'] = steps
    scenario['satellites'] = [satellite]
    source = scenario['environment']['S01']
    scenario['environment'] = {
        'S01': {
            key: ([True] * steps if key in ('downlink_available', 'relay_available')
                  else value[:steps])
            for key, value in source.items()
        }
    }
    scenario['failures'] = []
    scenario['model']['downlink_parallel_limit'] = 1
    scenario['jobs'] = copy.deepcopy(jobs)
    return scenario


def job(job_id: str, priority: int, value: float, work: int = 2, deadline: int = 3) -> dict:
    return {
        'id': job_id,
        'kind': 'downlink',
        'release_step': 0,
        'deadline_step': deadline,
        'work_steps': work,
        'eligible_satellites': ['S01'],
        'priority': priority,
        'value_usd': value,
    }


class StrategicTests(unittest.TestCase):
    def test_goals_choose_different_conflicting_jobs(self) -> None:
        scenario = compact_scenario([
            job('CRITICAL', 3, 1.0, work=2),
            job('COMMERCIAL', 1, 1000.0, work=1),
        ])
        scenario['jobs'][0]['kind'] = 'relay'
        scenario['jobs'][1]['kind'] = 'downlink'
        scenario['environment']['S01']['relay_available'] = [True, True, True]
        scenario['environment']['S01']['downlink_available'] = [True, False, False]
        priority = run_strategic(scenario, goal='priority')
        revenue = run_strategic(scenario, goal='revenue')
        self.assertEqual(priority.env.completed, ['CRITICAL'])
        self.assertEqual(revenue.env.completed, ['COMMERCIAL', 'CRITICAL'])

    def test_revenue_still_selects_urgent_critical_work(self) -> None:
        scenario = compact_scenario([
            job('URGENT', 3, 1.0, work=1, deadline=1),
            job('COMMERCIAL', 1, 1000.0, work=1, deadline=3),
        ])
        session = run_strategic(scenario, goal='revenue')
        self.assertEqual(session.env.completed[0], 'URGENT')

    def test_contact_scarcity_beats_sorting_baseline(self) -> None:
        scenario = compact_scenario([
            job('A_FLEXIBLE', 1, 10.0, work=1, deadline=3),
            job('Z_SCARCE', 1, 10.0, work=1, deadline=3),
        ])
        scenario['jobs'][0]['kind'] = 'downlink'
        scenario['jobs'][1]['kind'] = 'relay'
        scenario['environment']['S01']['downlink_available'] = [False, True, True]
        scenario['environment']['S01']['relay_available'] = [False, True, False]
        baseline = run_baseline(scenario)
        strategic = run_strategic(scenario, goal='priority')
        self.assertEqual(baseline.env.completed, ['A_FLEXIBLE'])
        self.assertEqual(strategic.env.completed, ['Z_SCARCE', 'A_FLEXIBLE'])

    def test_relay_executor_and_no_blocked_commands(self) -> None:
        scenario = load(P01)
        scenario['time']['steps'] = 3
        scenario['jobs'] = [dict(
            scenario['jobs'][6], release_step=0, deadline_step=3,
            work_steps=1, eligible_satellites=['S01', 'S02'],
        )]
        for environment in scenario['environment'].values():
            for key in environment:
                environment[key] = environment[key][:3]
            environment['relay_available'] = [True] * 3
        session = run_strategic(scenario, goal='priority')
        self.assertEqual(session.summary()['blocked_command_count'], 0)
        self.assertEqual(sum(job['action'] == 'job' for job in session.commands), 1)

    def test_relay_uses_free_executor_when_downlink_has_only_one(self) -> None:
        scenario = compact_scenario([
            job('RELAY', 1, 10.0, work=1),
            job('DOWNLINK', 1, 10.0, work=1),
        ], steps=2)
        satellite = copy.deepcopy(scenario['satellites'][0])
        satellite['id'] = 'S02'
        scenario['satellites'].append(satellite)
        scenario['environment']['S02'] = copy.deepcopy(scenario['environment']['S01'])
        scenario['jobs'][0]['kind'] = 'relay'
        scenario['jobs'][0]['eligible_satellites'] = ['S01', 'S02']
        scenario['jobs'][1]['eligible_satellites'] = ['S02']
        for item in scenario['jobs']:
            item['deadline_step'] = 2
        actions = StrategicPlanner('priority').plan(Session(scenario))
        self.assertEqual(actions['S01'], {'action': 'job', 'job_id': 'RELAY'})
        self.assertEqual(actions['S02'], {'action': 'job', 'job_id': 'DOWNLINK'})

    def test_known_failure_reduces_future_capacity(self) -> None:
        scenario = compact_scenario([
            job('IMPOSSIBLE', 1, 50.0, work=2),
            job('VIABLE', 1, 10.0, work=1),
        ])
        scenario['failures'] = [{'satellite_id': 'S01', 'start_step': 1, 'end_step': 3}]
        actions = StrategicPlanner('revenue').plan(Session(scenario))
        self.assertEqual(actions['S01'], {'action': 'job', 'job_id': 'VIABLE'})

    def test_future_executor_scarcity_preserves_a_later_window(self) -> None:
        scenario = compact_scenario([
            job('FLEXIBLE', 3, 10.0, work=1, deadline=1),
            job('SCARCE', 1, 20.0, work=1, deadline=2),
        ], steps=3)
        satellite = copy.deepcopy(scenario['satellites'][0])
        satellite['id'] = 'S02'
        scenario['satellites'].append(satellite)
        scenario['environment']['S02'] = copy.deepcopy(scenario['environment']['S01'])
        scenario['jobs'][0]['kind'] = 'relay'
        scenario['jobs'][0]['eligible_satellites'] = ['S01', 'S02']
        scenario['jobs'][1]['kind'] = 'downlink'
        scenario['jobs'][1]['eligible_satellites'] = ['S01']
        scenario['environment']['S01']['downlink_available'] = [False, True, False]
        scenario['satellites'][0]['initial_soc_pct'] = 39.0
        scenario['satellites'][1]['initial_soc_pct'] = 38.0
        for environment in scenario['environment'].values():
            environment['solar_w'] = [0.0] * 3

        baseline = run_baseline(scenario)
        strategic = run_strategic(scenario, goal='priority')

        self.assertNotIn('SCARCE', baseline.env.completed)
        self.assertIn('SCARCE', strategic.env.completed)
        self.assertEqual(strategic.summary()['blocked_command_count'], 0)

    def test_future_event_not_used_until_announced_and_replays(self) -> None:
        scenario = compact_scenario([job('DOWNLINK', 1, 10.0, work=2)])
        event = {
            'id': 'close', 'at_step': 1, 'type': 'close_downlink',
            'satellite_ids': ['S01'], 'end_step': 3,
        }
        for goal in ('priority', 'revenue'):
            with self.subTest(goal=goal):
                plain = run_strategic(scenario, goal=goal)
                affected = run_strategic(scenario, [event], goal)
                self.assertEqual(plain.commands[:1], affected.commands[:1])
                self.assertEqual(affected.summary()['blocked_command_count'], 0)
                self.assertEqual(
                    affected.summary(),
                    replay_episode(scenario, affected.events, affected.commands).summary(),
                )

    def test_cli_goal_and_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            for extra in (['--goal', 'revenue'], ['--compare', '--goal', 'revenue']):
                subprocess.run(
                    [sys.executable, '-m', 'planner.cli', '--scenario', str(P01),
                     '--output', str(output), *extra],
                    cwd=ROOT, check=True, capture_output=True, text=True,
                )
                payload = json.loads(output.read_text(encoding='utf-8'))
                if '--compare' in extra:
                    self.assertIn('delta', payload)
                    self.assertEqual(payload['goal'], 'revenue')
                else:
                    metadata = payload['run_metadata']
                    self.assertEqual(metadata['algorithm'], 'strategic')
                    self.assertEqual(metadata['goal'], 'revenue')
                    self.assertIn('parameters', metadata)
                    self.assertEqual(payload['summary']['blocked_command_count'], 0)

    def test_both_goals_replay_and_metadata(self) -> None:
        for goal in ('priority', 'revenue'):
            with self.subTest(goal=goal):
                scenario = load(ROOT / 'data' / 'P01_intro.json')
                session = run_strategic(scenario, goal=goal)
                replayed = replay_episode(
                    scenario, session.events, session.commands, session.env.k,
                )
                self.assertEqual(replayed.summary(), session.summary())
                self.assertEqual(replayed.env.trace, session.env.trace)
                self.assertEqual(session.run_metadata['algorithm'], 'strategic')
                self.assertEqual(session.run_metadata['goal'], goal)
                self.assertIn('version', session.run_metadata)
                self.assertIn('parameters', session.run_metadata)
                self.assertTrue(session.run_metadata['parameters']['future_executor_scarcity'])
                self.assertEqual(
                    session.run_metadata['parameters']['executor_energy_score'],
                    'post_action_soc',
                )
                self.assertEqual(session.summary()['blocked_command_count'], 0)

    def test_comparison_contains_machine_readable_delta(self) -> None:
        scenario = compact_scenario([
            job('CRITICAL', 3, 1.0, work=2),
            job('COMMERCIAL', 1, 1000.0, work=1),
        ])
        scenario['jobs'][0]['kind'] = 'relay'
        scenario['jobs'][1]['kind'] = 'downlink'
        scenario['environment']['S01']['relay_available'] = [True, True, True]
        scenario['environment']['S01']['downlink_available'] = [True, False, False]
        comparison = compare_scenario(scenario, goal='revenue')
        self.assertEqual(comparison['schema_version'], 'cosmo-B-planner-comparison-1.0')
        self.assertEqual(comparison['delta']['revenue_usd'], 1000.0)
        self.assertEqual(comparison['delta']['blocked_command_count'], 0)


if __name__ == '__main__':
    unittest.main()
