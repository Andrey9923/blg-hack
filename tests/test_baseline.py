from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from model.operations import Session, replay_episode
from model.resource_env import load
from planner.baseline import BaselinePlanner, run_baseline


ROOT = Path(__file__).resolve().parents[1]
P01 = ROOT / 'data' / 'P01_intro.json'


class BaselineTests(unittest.TestCase):
    def test_plan_covers_satellites_and_resolves_conflicts(self) -> None:
        scenario = load(P01)
        session = run_baseline(scenario)

        self.assertEqual(session.env.k, scenario['time']['steps'])
        self.assertEqual(session.summary()['blocked_command_count'], 0)

        for step in range(scenario['time']['steps']):
            step_commands = [
                command for command in session.commands if command['step'] == step
            ]
            self.assertEqual(
                {command['satellite_id'] for command in step_commands},
                set(session.env.sats),
            )
            jobs = [
                command['job_id'] for command in step_commands
                if command['action'] == 'job'
            ]
            self.assertEqual(len(jobs), len(set(jobs)))
            downlinks = [
                command for command in step_commands
                if command['action'] == 'job'
                and session.env.jobs[command['job_id']]['kind'] == 'downlink'
            ]
            self.assertLessEqual(
                len(downlinks), scenario['model']['downlink_parallel_limit']
            )

    def test_replay_matches_summary_and_trace(self) -> None:
        scenario = load(P01)
        session = run_baseline(scenario)
        replayed = replay_episode(
            scenario,
            session.events,
            session.commands,
            session.env.k,
        )

        self.assertEqual(replayed.summary(), session.summary())
        self.assertEqual(replayed.env.trace, session.env.trace)

    def test_expired_calibration_is_selected_before_a_job(self) -> None:
        scenario = load(P01)
        session = Session(scenario)
        session.env.state['S01']['calibration_age_steps'] = scenario['model']['calibration_valid_steps']

        actions = BaselinePlanner().plan(session)

        self.assertEqual(actions['S01'], {'action': 'calibrate'})

    def test_priority_release_eligibility_and_ground_capacity(self) -> None:
        scenario = load(P01)
        scenario['time']['steps'] = 3
        scenario['model']['downlink_parallel_limit'] = 1
        for environment in scenario['environment'].values():
            for key in environment:
                environment[key] = environment[key][:3]
            environment['downlink_available'] = [True] * 3
            environment['relay_available'] = [True] * 3

        template = scenario['jobs'][0]
        scenario['jobs'] = []
        for job_id, sid, priority, release in (
            ('LOW', 'S01', 1, 0),
            ('CRITICAL', 'S02', 3, 0),
            ('FUTURE', 'S03', 3, 1),
        ):
            scenario['jobs'].append(dict(
                template, id=job_id, kind='downlink', release_step=release,
                deadline_step=3, work_steps=1, eligible_satellites=[sid],
                priority=priority,
            ))

        session = Session(scenario)
        actions = BaselinePlanner().plan(session)
        self.assertEqual(actions['S02'], {'action': 'job', 'job_id': 'CRITICAL'})
        self.assertEqual(actions['S01'], {'action': 'idle'})
        self.assertEqual(actions['S03'], {'action': 'idle'})
        session.advance(actions)
        actions = BaselinePlanner().plan(session)
        self.assertEqual(actions['S03'], {'action': 'job', 'job_id': 'FUTURE'})
        self.assertEqual(actions['S01'], {'action': 'idle'})
        self.assertEqual(session.summary()['blocked_command_count'], 0)

    def test_shared_relay_job_is_assigned_only_once(self) -> None:
        scenario = load(P01)
        scenario['jobs'] = [dict(
            scenario['jobs'][6], release_step=0, deadline_step=3,
            work_steps=1, eligible_satellites=['S01', 'S02'],
        )]
        scenario['time']['steps'] = 3
        for environment in scenario['environment'].values():
            for key in environment:
                environment[key] = environment[key][:3]
            environment['relay_available'] = [True] * 3

        actions = BaselinePlanner().plan(Session(scenario))
        assigned = [sid for sid, action in actions.items() if action['action'] == 'job']
        self.assertEqual(len(assigned), 1)
        self.assertIn(assigned[0], ['S01', 'S02'])

    def test_all_scenarios_complete_without_blocked_commands(self) -> None:
        for name in ('P01_intro', 'P02_shift', 'P03_energy', 'P04_demand'):
            with self.subTest(scenario=name):
                scenario = load(ROOT / 'data' / f'{name}.json')
                session = run_baseline(scenario)
                self.assertEqual(session.env.k, scenario['time']['steps'])
                self.assertEqual(session.summary()['blocked_command_count'], 0)

    def test_cli_writes_a_session_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'p01-result.json'
            completed = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'planner.cli',
                    '--scenario',
                    str(P01),
                    '--output',
                    str(output),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('steps_executed', completed.stdout)
            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(result['schema_version'], 'cosmo-B-ops-result-1.0')
            self.assertEqual(result['steps_executed'], 48)
            replayed = replay_episode(
                result['initial_scenario'], result['events'], result['commands'],
                result['steps_executed'],
            )
            self.assertEqual(replayed.summary(), result['summary'])
            self.assertEqual(replayed.env.trace, result['trace'])

    def test_announced_events_replay(self) -> None:
        scenario = load(ROOT / 'data' / 'P02_shift.json')
        event_file = json.loads(
            (ROOT / 'examples' / 'events_demo.json').read_text(encoding='utf-8')
        )
        session = run_baseline(scenario, event_file['events'])
        replayed = replay_episode(
            scenario, session.events, session.commands, session.env.k,
        )
        self.assertEqual(session.summary()['blocked_command_count'], 0)
        self.assertEqual(replayed.summary(), session.summary())
        self.assertEqual(replayed.env.trace, session.env.trace)


if __name__ == '__main__':
    unittest.main()
