from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .resource_env import Environment, load, validate
except ImportError:
    from resource_env import Environment, load, validate

EVENT_SCHEMA = 'cosmo-B-events-1.0'
RESULT_SCHEMA = 'cosmo-B-ops-result-1.0'


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f'{name} must be an integer')
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class Session:
    """One executed history. Only received events are held in this object.

    ``env`` exposes the environment API for optional direct integration.
    It contains the initial known plan, not an undisclosed future event script.
    This is an integration library, not a sandbox or anti-cheating boundary.
    """
    def __init__(self, scenario: dict, run_metadata: dict | None = None):
        validate(scenario)
        self.run_metadata = copy.deepcopy(run_metadata or {})
        self.initial_scenario = copy.deepcopy(scenario)
        self.env = Environment(scenario)
        self.events: list[dict] = []
        self.commands: list[dict] = []
        self._event_ids: set[str] = set()

    def observation(self) -> dict:
        return self.env.observation()

    def state_digest(self) -> str:
        return digest({'step': self.env.k, 'state': self.env.state,
                       'jobs': self.env.jobs, 'completed': self.env.completed})

    def fork(self) -> Session:
        """Copy the same actual state and executed prefix for a comparison."""
        return copy.deepcopy(self)

    def apply_event(self, event: dict) -> None:
        """Accept an event exactly at its announced step, before that step's action.

        Intervals are [at_step, end_step). Both boundaries refer to 5-minute steps.
        End-of-outage times are announced with the event in this model.
        Validation is atomic: malformed events do not partially update the state.
        """
        if not isinstance(event, dict):
            raise ValueError('Event must be an object')
        e = copy.deepcopy(event)
        eid = e.get('id')
        if not isinstance(eid, str) or not eid or eid in self._event_ids:
            raise ValueError('Event id must be a new nonempty string')
        at = _integer(e.get('at_step'), 'at_step')
        if at != self.env.k or not 0 <= at < self.env.s['time']['steps']:
            raise ValueError('Event must arrive at the current unfinished step')
        typ = e.get('type')
        required = {'id', 'at_step', 'type'}
        candidate = copy.deepcopy(self.env.s)
        new_jobs: list[dict] = []
        if typ == 'add_jobs':
            if set(e) != required | {'jobs'}:
                raise ValueError('add_jobs requires exactly id, at_step, type, jobs')
            if not isinstance(e['jobs'], list) or not e['jobs']:
                raise ValueError('jobs must be a nonempty list')
            new_jobs = e['jobs']
            for j in new_jobs:
                if not isinstance(j, dict):
                    raise ValueError('Every job must be an object')
                if _integer(j.get('release_step'), 'release_step') < at:
                    raise ValueError('A new job cannot be released before announcement')
                if set(j) != {'id', 'kind', 'release_step', 'deadline_step',
                             'work_steps', 'eligible_satellites', 'priority', 'value_usd'}:
                    raise ValueError('New job fields must match the published job schema')
            candidate['jobs'].extend(new_jobs)
        elif typ in ('satellite_outage', 'close_downlink'):
            if set(e) != required | {'satellite_ids', 'end_step'}:
                raise ValueError('Outage fields must match the published event schema')
            end = _integer(e['end_step'], 'end_step')
            if not at < end <= candidate['time']['steps']:
                raise ValueError('Invalid outage interval')
            ids = e['satellite_ids']
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
                raise ValueError('satellite_ids must be a nonempty list of strings')
            if len(ids) != len(set(ids)) or not set(ids) <= set(self.env.sats):
                raise ValueError('Unknown or duplicate satellite in event')
            if typ == 'satellite_outage':
                candidate['failures'].extend(
                    {'satellite_id': sid, 'start_step': at, 'end_step': end} for sid in ids)
            else:
                for sid in ids:
                    candidate['environment'][sid]['downlink_available'][at:end] = [False] * (end-at)
        else:
            raise ValueError('Unsupported event type')
        validate(candidate)
        # No reset of energy, temperature, calibration, progress or time.
        self.env.s = candidate
        for j in new_jobs:
            self.env.jobs[j['id']] = dict(copy.deepcopy(j), remaining_steps=j['work_steps'], completed_step=None)
        self.events.append(copy.deepcopy(e))
        self._event_ids.add(eid)

    def advance(self, actions: dict[str, dict]) -> list[dict]:
        if not isinstance(actions, dict):
            raise ValueError('Actions must be a dictionary')
        if set(actions) - set(self.env.sats):
            raise ValueError('Unknown satellite in actions')
        for sid, a in actions.items():
            if isinstance(a, dict) and a.get('action') not in ('idle', 'calibrate', 'job'):
                raise ValueError(f'Unknown action for {sid}')
            if not isinstance(a, dict) or not isinstance(a.get('action'), str):
                raise ValueError(f'Invalid action for {sid}')
            expected = {'action', 'job_id'} if a['action'] == 'job' else {'action'}
            if set(a) != expected or ('job_id' in a and not isinstance(a['job_id'], str)):
                raise ValueError(f'Invalid action fields for {sid}')
        saved = copy.deepcopy(actions)
        step = self.env.k
        rows = self.env.step(saved)
        self.commands.extend(dict(a, step=step, satellite_id=sid) for sid, a in saved.items())
        return rows

    def summary(self) -> dict:
        s = self.env.summary()
        s['terminal_soc_pct'] = {sid: round(100 * state['energy_wh'] /
                                           self.env.sats[sid]['capacity_wh'], 6)
                                  for sid, state in self.env.state.items()}
        s['work_steps_in_missed_jobs'] = sum(
            j['work_steps']-j['remaining_steps'] for j in self.env.jobs.values()
            if j['deadline_step'] <= self.env.k and j['completed_step'] is None)
        return s

    def result(self) -> dict:
        return copy.deepcopy({'schema_version': RESULT_SCHEMA, 'run_metadata': self.run_metadata,
            'initial_scenario': self.initial_scenario, 'initial_scenario_hash': digest(self.initial_scenario),
            'events': self.events, 'commands': self.commands,
            'steps_executed': self.env.k, 'summary': self.summary(), 'trace': self.env.trace})


def replay_episode(scenario: dict, events: list[dict], commands: list[dict],
                   until_step: int | None = None) -> Session:
    """Recompute a saved execution; this checks accounting, not advance knowledge."""
    n = scenario['time']['steps']
    stop = n if until_step is None else _integer(until_step, 'until_step')
    if not 0 <= stop <= n:
        raise ValueError('Invalid stopping step')
    if not isinstance(events, list) or not isinstance(commands, list):
        raise ValueError('Events and commands must be lists')
    event_map: dict[int, list[dict]] = {}
    previous_at = -1
    event_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('Event must be an object')
        k = _integer(event.get('at_step'), 'at_step')
        eid = event.get('id')
        if not 0 <= k < n or not isinstance(eid, str) or not eid or eid in event_ids:
            raise ValueError('Invalid or duplicate event record')
        if k > stop or k < previous_at:
            raise ValueError('Events must be in receipt order and not after the saved state')
        previous_at = k
        event_ids.add(eid)
        event_map.setdefault(k, []).append(event)
    command_map: dict[int, dict] = {}
    for command in commands:
        if not isinstance(command, dict):
            raise ValueError('Command must be an object')
        k = _integer(command.get('step'), 'step')
        if not 0 <= k < stop:
            raise ValueError('Command must precede the saved state')
        sid = command.get('satellite_id')
        if not isinstance(sid, str) or sid in command_map.setdefault(k, {}):
            raise ValueError('Invalid/duplicate satellite command')
        command_map[k][sid] = {key: val for key, val in command.items()
                               if key not in ('step', 'satellite_id')}
    session = Session(scenario)
    while session.env.k < stop:
        k = session.env.k
        for event in event_map.get(k, []):
            session.apply_event(event)
        session.advance(command_map.get(k, {}))
    # A snapshot taken after receiving an event, but before taking an action,
    # must retain those boundary events as well.
    if stop < n:
        for event in event_map.get(stop, []):
            session.apply_event(event)
    return session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, help='Replay a complete exported result')
    parser.add_argument('--scenario', type=Path)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--commands', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.result:
            if any((args.scenario, args.events, args.commands)):
                raise ValueError('Use --result or separate inputs, not both')
            record = json.loads(args.result.read_text(encoding='utf-8-sig'))
            if record.get('schema_version') != RESULT_SCHEMA:
                raise ValueError('Unsupported result schema')
            session = replay_episode(record['initial_scenario'], record['events'],
                                     record['commands'], record['steps_executed'])
            session.run_metadata = copy.deepcopy(record.get('run_metadata', {}))
        else:
            if args.scenario is None or args.commands is None:
                raise ValueError('Provide --result or both --scenario and --commands')
            scenario = load(args.scenario)
            events = []
            if args.events:
                event_file = json.loads(args.events.read_text(encoding='utf-8-sig'))
                if event_file.get('schema_version') != EVENT_SCHEMA:
                    raise ValueError('Unsupported events schema')
                if event_file.get('base_scenario') != scenario['meta']['id']:
                    raise ValueError('Events refer to a different base scenario')
                events = event_file['events']
            command_file = json.loads(args.commands.read_text(encoding='utf-8-sig'))
            commands = command_file.get('commands') if isinstance(command_file, dict) else command_file
            session = replay_episode(scenario, events, commands)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(session.result(), ensure_ascii=False, indent=2,
                                         allow_nan=False), encoding='utf-8')
        print(json.dumps(session.summary(), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f'Cannot replay episode: {exc}\n')


if __name__ == '__main__':
    main()
