from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from model.resource_env import load
from planner.runtime import PlannerRuntime


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def test_event_is_only_accepted_at_current_boundary(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        runtime = PlannerRuntime(scenario, planner='strategic')
        runtime.run_until(1)
        before = copy.deepcopy(runtime.observation())
        event = {
            'id': 'future-outage', 'at_step': 2, 'type': 'satellite_outage',
            'satellite_ids': ['S01'], 'end_step': 4,
        }

        with self.assertRaisesRegex(ValueError, 'current unfinished step'):
            runtime.apply_event(event)

        self.assertEqual(runtime.current_step, 1)
        self.assertEqual(runtime.events, [])
        self.assertEqual(runtime.history, [entry for entry in runtime.history if entry['type'] == 'step'])
        self.assertEqual(runtime.observation()['state'], before['state'])
        self.assertEqual(runtime.observation()['jobs'], before['jobs'])

    def test_bad_event_is_atomic_and_outage_preserves_local_state(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        runtime = PlannerRuntime(scenario)
        runtime.run_until(1)
        state_before = copy.deepcopy(runtime.env.state)
        jobs_before = copy.deepcopy(runtime.env.jobs)
        scenario_before = copy.deepcopy(runtime.env.s)
        history_before = copy.deepcopy(runtime.history)

        bad = {
            'id': 'bad', 'at_step': 1, 'type': 'close_downlink',
            'satellite_ids': ['S01', 'S01'], 'end_step': 4,
        }
        with self.assertRaisesRegex(ValueError, 'Unknown or duplicate'):
            runtime.apply_event(bad)

        self.assertEqual(runtime.env.state, state_before)
        self.assertEqual(runtime.env.jobs, jobs_before)
        self.assertEqual(runtime.env.s, scenario_before)
        self.assertEqual(runtime.history, history_before)
        self.assertEqual(runtime.events, [])

        runtime.apply_event({
            'id': 'outage', 'at_step': 1, 'type': 'satellite_outage',
            'satellite_ids': ['S01'], 'end_step': 4,
        })
        self.assertEqual(runtime.env.state, state_before)
        self.assertEqual(runtime.env.jobs, jobs_before)
        self.assertFalse(runtime.env.available('S01'))

    def test_add_jobs_and_goal_switch_are_recorded(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        runtime = PlannerRuntime(scenario, planner='strategic', goal='priority')
        runtime.run_until(1)
        runtime.apply_event({
            'id': 'new-job', 'at_step': 1, 'type': 'add_jobs',
            'jobs': [{
                'id': 'RUNTIME-JOB', 'kind': 'relay', 'release_step': 1,
                'deadline_step': 3, 'work_steps': 1,
                'eligible_satellites': ['S01'], 'priority': 3,
                'value_usd': 12,
            }],
        })
        runtime.switch_goal('revenue')

        self.assertIn('RUNTIME-JOB', runtime.env.jobs)
        self.assertEqual(runtime.commands[0]['step'], 0)
        self.assertEqual(runtime.run_metadata['goal'], 'revenue')
        self.assertEqual(runtime.run_metadata['goal_switches'], [{
            'at_step': 1, 'from': 'priority', 'to': 'revenue',
        }])
        self.assertEqual([item['type'] for item in runtime.history], [
            'step', 'event', 'goal_switch',
        ])

    def test_forks_are_independent_and_replayable(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        satellite = copy.deepcopy(scenario['satellites'][0])
        satellite['initial_calibration_age_steps'] = 0
        scenario['time']['steps'] = 3
        scenario['satellites'] = [satellite]
        source = scenario['environment']['S01']
        scenario['environment'] = {
            'S01': {
                key: ([True] * 3 if key in ('downlink_available', 'relay_available')
                      else value[:3])
                for key, value in source.items()
            }
        }
        scenario['jobs'] = []
        scenario['failures'] = []
        parent = PlannerRuntime(scenario, planner='strategic', goal='priority')
        parent.run_until(1)
        first = parent.fork('added-job', goal='revenue')
        second = parent.fork('outage', goal='priority')

        self.assertEqual(first.state_digest(), second.state_digest())
        self.assertEqual(first.run_metadata['parent_run_id'], parent.run_metadata['run_id'])
        self.assertEqual(first.run_metadata['parent_branch_point']['step'], 1)

        first.apply_event({
            'id': 'branch-job', 'at_step': 1, 'type': 'add_jobs',
            'jobs': [{
                'id': 'BRANCH-JOB', 'kind': 'relay', 'release_step': 1,
                'deadline_step': 3, 'work_steps': 1,
                'eligible_satellites': ['S01'], 'priority': 3,
                'value_usd': 30,
            }],
        })
        second.apply_event({
            'id': 'branch-outage', 'at_step': 1, 'type': 'satellite_outage',
            'satellite_ids': ['S01'], 'end_step': 3,
        })
        first.step()
        second.step()

        self.assertEqual(parent.current_step, 1)
        self.assertNotEqual(
            first.commands[-len(scenario['satellites']):],
            second.commands[-len(scenario['satellites']):],
        )
        for branch in (first, second):
            replayed = branch.replay()
            self.assertEqual(replayed.summary(), branch.session.summary())
            self.assertEqual(replayed.env.trace, branch.session.env.trace)
            self.assertEqual(replayed.state_digest(), branch.state_digest())

    def test_manual_example_events_work_without_future_leakage(self) -> None:
        scenario = load(ROOT / 'data' / 'P02_shift.json')
        document = json.loads((ROOT / 'examples' / 'events_demo.json').read_text())
        runtime = PlannerRuntime(scenario, planner='strategic', goal='priority')
        events = {event['at_step']: event for event in document['events']}
        for boundary in sorted(events):
            runtime.run_until(boundary)
            self.assertEqual(runtime.current_step, boundary)
            runtime.apply_event(events[boundary])
        runtime.run()

        self.assertEqual(runtime.current_step, scenario['time']['steps'])
        self.assertEqual(runtime.summary()['blocked_command_count'], 0)
        self.assertEqual(len(runtime.events), len(events))

    def test_result_has_json_prefix_only(self) -> None:
        scenario = load(ROOT / 'data' / 'P01_intro.json')
        runtime = PlannerRuntime(scenario)
        runtime.run_steps(2)
        payload = json.loads(runtime.to_json())
        self.assertEqual(payload['steps_executed'], 2)
        self.assertEqual(len(payload['commands']), 2 * len(scenario['satellites']))
        self.assertNotIn('future_plan', payload)


if __name__ == '__main__':
    unittest.main()
