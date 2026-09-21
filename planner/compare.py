from __future__ import annotations

import copy
from typing import Any, Iterable

from .baseline import run_baseline
from .strategic import run_strategic


COMPARISON_METRICS = (
    'jobs_completed',
    'jobs_due_missed',
    'critical_jobs_completed_on_time',
    'revenue_usd',
    'blocked_command_count',
    'below_reserve_satellite_steps',
    'brownout_satellite_steps',
    'minimum_soc_pct',
)


def compare_scenario(
    scenario: dict,
    events: Iterable[dict[str, Any]] = (),
    goal: str = 'priority',
) -> dict[str, Any]:
    """Run both policies on the same scenario/events and return JSON data."""
    announced_events = [copy.deepcopy(event) for event in events]
    baseline = run_baseline(scenario, announced_events).summary()
    strategic = run_strategic(scenario, announced_events, goal).summary()
    return {
        'schema_version': 'cosmo-B-planner-comparison-1.0',
        'scenario_id': scenario['meta']['id'],
        'goal': goal,
        'baseline': baseline,
        'strategic': strategic,
        'delta': {
            metric: strategic[metric] - baseline[metric]
            for metric in COMPARISON_METRICS
        },
    }
