"""Reproducible, compact planner experiments.

The runner submits an event only when the runtime reaches that event's step.
It therefore compares policies on the same received event prefix without
giving either planner a future event queue.
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import time
from pathlib import Path
from typing import Any, Iterable

from model.resource_env import load

from .runtime import PlannerRuntime


ANALYSIS_SCHEMA = 'cosmo-B-planner-analysis-1.0'
METRICS = (
    'jobs_completed',
    'jobs_due_missed',
    'critical_jobs_completed_on_time',
    'revenue_usd',
    'blocked_command_count',
    'below_reserve_satellite_steps',
    'brownout_satellite_steps',
    'critical_soc_satellite_steps',
    'minimum_soc_pct',
)
RESOURCE_METRICS = (
    'below_reserve_satellite_steps',
    'brownout_satellite_steps',
    'critical_soc_satellite_steps',
    'minimum_soc_pct',
)
_RESOURCE_REASONS = {
    'energy_reserve',
    'thermal_limit',
    'satellite_unavailable',
    'no_contact',
    'ground_capacity',
}


def run_runtime(
    scenario: dict[str, Any],
    planner: str,
    goal: str,
    events: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Run one policy while delivering events at their actual boundaries."""
    runtime = PlannerRuntime(scenario, planner=planner, goal=goal)
    started = time.perf_counter()
    received_events = [copy.deepcopy(event) for event in events]
    for event in received_events:
        runtime.run_until(event['at_step'])
        runtime.apply_event(event)
    runtime.run()
    elapsed = time.perf_counter() - started
    summary = runtime.summary()
    reason_counts = collections.Counter(
        row['reason'] for row in runtime.session.env.trace
    )
    unfinished = [
        {
            'id': job['id'],
            'remaining_steps': job['remaining_steps'],
            'deadline_step': job['deadline_step'],
            'priority': job['priority'],
            'value_usd': job['value_usd'],
            'status': 'deadline_missed' if job['deadline_step'] <= runtime.current_step else 'unfinished',
        }
        for job in sorted(runtime.env.jobs.values(), key=lambda item: item['id'])
        if job['completed_step'] is None
    ]
    accepted = reason_counts.get('accepted', 0)
    idle = reason_counts.get('idle', 0)
    rejected = sum(
        count for reason, count in reason_counts.items()
        if reason not in ('accepted', 'idle')
    )
    resource_rejected = sum(
        count for reason, count in reason_counts.items()
        if reason in _RESOURCE_REASONS
    )
    return {
        'summary': summary,
        'metrics': copy.deepcopy(summary),
        'execution': {
            'seconds': round(elapsed, 6),
            'steps': runtime.current_step,
            'commands': len(runtime.commands),
            'events_received': len(runtime.events),
        },
        'reason_counts': dict(sorted(reason_counts.items())),
        'losses': {
            'accepted_trace_rows': accepted,
            'idle_trace_rows': idle,
            'rejected_command_trace_rows': rejected,
            'resource_rejected_trace_rows': resource_rejected,
            'deadline_missed_jobs': sum(item['status'] == 'deadline_missed' for item in unfinished),
            'unfinished_jobs': len(unfinished),
        },
        'unfinished_jobs': unfinished,
        'resource_indicators': {
            key: summary[key] for key in RESOURCE_METRICS
        },
    }


def analyze_scenario(
    scenario: dict[str, Any],
    events: Iterable[dict[str, Any]] = (),
    goal: str = 'priority',
) -> dict[str, Any]:
    """Return a JSON-compatible baseline/strategic experiment report."""
    announced_events = [copy.deepcopy(event) for event in events]
    baseline = run_runtime(scenario, 'baseline', goal, announced_events)
    strategic = run_runtime(scenario, 'strategic', goal, announced_events)
    delta = {
        metric: round(strategic['summary'][metric] - baseline['summary'][metric], 6)
        for metric in METRICS
    }
    delta['execution_seconds'] = round(
        strategic['execution']['seconds'] - baseline['execution']['seconds'], 6
    )
    return {
        'schema_version': ANALYSIS_SCHEMA,
        'scenario_id': scenario['meta']['id'],
        'goal': goal,
        'received_events': announced_events,
        'metrics': {
            'baseline': copy.deepcopy(baseline['metrics']),
            'strategic': copy.deepcopy(strategic['metrics']),
        },
        'baseline': baseline,
        'strategic': strategic,
        'delta': delta,
        'interpretation': {
            'scope': 'This compares the recorded greedy executions, not objective feasibility.',
            'positive_delta': 'strategic minus baseline',
        },
    }


def _read_events(path: Path | None, scenario_id: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    if document.get('schema_version') != 'cosmo-B-events-1.0':
        raise ValueError('Unsupported events schema')
    if document.get('base_scenario') != scenario_id:
        raise ValueError('Events refer to a different base scenario')
    events = document.get('events')
    if not isinstance(events, list):
        raise ValueError('Events must be a list')
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description='Compare deterministic planner executions.')
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--goal', choices=('priority', 'revenue'), default='priority')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        scenario = load(args.scenario)
        events = _read_events(args.events, scenario['meta']['id'])
        report = analyze_scenario(scenario, events, args.goal)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding='utf-8')
        print(encoded)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'Cannot analyze planner: {exc}\n')


if __name__ == '__main__':
    main()
