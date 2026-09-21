from __future__ import annotations

import argparse
import json
from pathlib import Path

from model.resource_env import load

from .baseline import run_baseline
from .compare import compare_scenario
from .strategic import run_strategic


EVENT_SCHEMA = 'cosmo-B-events-1.0'


def _read_events(path: Path | None, scenario_id: str) -> list[dict]:
    if path is None:
        return []
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    if document.get('schema_version') != EVENT_SCHEMA:
        raise ValueError('Unsupported events schema')
    if document.get('base_scenario') != scenario_id:
        raise ValueError('Events refer to a different base scenario')
    events = document.get('events')
    if not isinstance(events, list):
        raise ValueError('Events must be a list')
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description='Run a deterministic satellite planner.')
    parser.add_argument('--scenario', type=Path, required=True, help='Scenario JSON, for example data/P01_intro.json')
    parser.add_argument('--events', type=Path, help='Optional events JSON')
    parser.add_argument('--output', type=Path, required=True, help='Output Session.result() JSON')
    parser.add_argument('--planner', choices=('baseline', 'strategic'),
                        help='Planner to run; --goal implies strategic')
    parser.add_argument('--goal', choices=('priority', 'revenue'),
                        help='Strategic objective (default: priority)')
    parser.add_argument('--compare', action='store_true',
                        help='Write a machine-readable baseline/strategic comparison')
    args = parser.parse_args()
    if args.planner == 'baseline' and args.goal:
        parser.error('--goal requires the strategic planner')
    if args.compare and args.planner:
        parser.error('--compare runs both planners; omit --planner')

    try:
        scenario = load(args.scenario)
        events = _read_events(args.events, scenario['meta']['id'])
        if args.compare:
            comparison = compare_scenario(scenario, events, args.goal or 'priority')
            payload = comparison
            printed = comparison
        else:
            planner_name = args.planner or ('strategic' if args.goal else 'baseline')
            if planner_name == 'strategic':
                session = run_strategic(
                    scenario, events, args.goal or 'priority',
                    {'scenario_id': scenario['meta']['id']},
                )
            else:
                session = run_baseline(
                    scenario,
                    events,
                    {'planner': 'baseline-v1', 'scenario_id': scenario['meta']['id']},
                )
            payload = session.result()
            printed = session.summary()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding='utf-8',
        )
        print(json.dumps(printed, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'Cannot run planner: {exc}\n')


if __name__ == '__main__':
    main()
