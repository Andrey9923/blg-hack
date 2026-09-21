from __future__ import annotations

import argparse
import json
from pathlib import Path

from model.resource_env import load

from .baseline import run_baseline


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
    parser = argparse.ArgumentParser(description='Run the deterministic baseline policy.')
    parser.add_argument('--scenario', type=Path, required=True, help='Scenario JSON, for example data/P01_intro.json')
    parser.add_argument('--events', type=Path, help='Optional events JSON')
    parser.add_argument('--output', type=Path, required=True, help='Output Session.result() JSON')
    args = parser.parse_args()

    try:
        scenario = load(args.scenario)
        events = _read_events(args.events, scenario['meta']['id'])
        session = run_baseline(
            scenario,
            events,
            {'planner': 'baseline-v1', 'scenario_id': scenario['meta']['id']},
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(session.result(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding='utf-8',
        )
        print(json.dumps(session.summary(), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'Cannot run baseline: {exc}\n')


if __name__ == '__main__':
    main()
