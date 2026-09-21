from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable

from model.operations import Session


IDLE = {'action': 'idle'}


class BaselinePlanner:
    """A deterministic, feasibility-first greedy policy.

    The policy makes one assignment pass per step.  It first calibrates every
    satellite whose calibration is no longer valid, then considers released
    jobs in this order:

    1. critical/high priority and least remaining slack;
    2. higher value, then stable job-id order.

    A satellite and a job are reserved as soon as an assignment is selected,
    and the downlink capacity is reserved globally.  Environment.can_execute
    remains the final local feasibility check for contact, energy and thermal
    limits.
    """

    def plan(self, session: Session) -> dict[str, dict[str, str]]:
        """Return valid, conflict-free actions for the current session step."""
        env = session.env
        step = env.k
        actions = {sid: copy.deepcopy(IDLE) for sid in sorted(env.sats)}
        reserved_satellites: set[str] = set()

        # A job cannot be accepted while calibration is expired, so repair
        # that local prerequisite before assigning shared work.
        for sid in sorted(env.sats):
            if not env.available(sid):
                continue
            if env.state[sid]['calibration_age_steps'] < env.s['model']['calibration_valid_steps']:
                continue
            candidate = {'action': 'calibrate'}
            if env.can_execute(sid, candidate)[0]:
                actions[sid] = candidate
                reserved_satellites.add(sid)

        jobs = sorted(
            (
                job for job in env.jobs.values()
                if job['release_step'] <= step
                and step < job['deadline_step']
                and job['completed_step'] is None
                and job['remaining_steps'] > 0
            ),
            key=lambda job: (
                -(job['priority'] == 3),
                -job['priority'],
                job['deadline_step'] - step - job['remaining_steps'],
                job['deadline_step'],
                -job['value_usd'],
                job['id'],
            ),
        )

        downlinks = 0
        downlink_limit = env.s['model']['downlink_parallel_limit']
        for job in jobs:
            if job['kind'] == 'downlink' and downlinks >= downlink_limit:
                continue

            action = {'action': 'job', 'job_id': job['id']}
            candidates: list[tuple[float, str]] = []
            for sid in sorted(job['eligible_satellites']):
                if sid in reserved_satellites or not env.available(sid):
                    continue
                ok, _, _ = env.can_execute(sid, action)
                if not ok:
                    continue
                # Prefer the satellite with more energy, preserving the
                # lower-energy units for later work when both are eligible.
                soc = env.state[sid]['energy_wh'] / env.sats[sid]['capacity_wh']
                candidates.append((-soc, sid))

            if not candidates:
                continue
            _, sid = min(candidates)
            actions[sid] = action
            reserved_satellites.add(sid)
            downlinks += int(job['kind'] == 'downlink')

        return actions


def run_baseline(
    scenario: dict,
    events: Iterable[dict[str, Any]] = (),
    run_metadata: dict[str, Any] | None = None,
) -> Session:
    """Execute the baseline for a scenario and optional announced events."""
    session = Session(scenario, run_metadata or {'planner': 'baseline-v1'})
    event_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_map[event['at_step']].append(copy.deepcopy(event))

    planner = BaselinePlanner()
    while session.env.k < scenario['time']['steps']:
        for event in event_map.get(session.env.k, []):
            session.apply_event(event)
        session.advance(planner.plan(session))
    return session
