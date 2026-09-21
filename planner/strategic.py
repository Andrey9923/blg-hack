from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Any, Iterable

from model.operations import Session


IDLE = {'action': 'idle'}
STRATEGIC_VERSION = 'strategic-v1'


class StrategicPlanner:
    """Deterministic rolling-horizon planner.

    The planner does not look beyond the current observation.  The scenario
    data already received by the session is used to count future contact
    windows, while a local state projection checks energy and thermal
    feasibility for each possible executor.  Assignment remains greedy, but
    it reserves satellites, jobs and downlink capacity before returning.
    """

    def __init__(self, goal: str = 'priority'):
        if goal not in ('priority', 'revenue'):
            raise ValueError("goal must be 'priority' or 'revenue'")
        self.goal = goal
        self.parameters = {
            'horizon': 'job_deadline',
            'resource_projection': 'work_when_feasible_else_idle',
            'priority_3_protection': True,
        }

    def plan(self, session: Session) -> dict[str, dict[str, str]]:
        env = session.env
        step = env.k
        actions = {sid: copy.deepcopy(IDLE) for sid in sorted(env.sats)}
        reserved_satellites: set[str] = set()
        reserved_jobs: set[str] = set()

        # Calibration is a prerequisite, not a job preference.  Repair every
        # expired unit that can be repaired now; this also makes the returned
        # action set safe when a future contact is the only useful window.
        for sid in sorted(env.sats):
            if not env.available(sid):
                continue
            if env.state[sid]['calibration_age_steps'] < env.s['model']['calibration_valid_steps']:
                continue
            candidate = {'action': 'calibrate'}
            if env.can_execute(sid, candidate)[0]:
                actions[sid] = candidate
                reserved_satellites.add(sid)

        jobs = [
            job for job in env.jobs.values()
            if job['release_step'] <= step
            and step < job['deadline_step']
            and job['completed_step'] is None
            and job['remaining_steps'] > 0
        ]
        profiles = {job['id']: self._profile(env, job) for job in jobs}
        jobs.sort(key=lambda job: self._job_key(job, profiles[job['id']], step))

        downlinks = 0
        downlink_limit = env.s['model']['downlink_parallel_limit']
        for job in jobs:
            if job['id'] in reserved_jobs:
                continue
            if job['kind'] == 'downlink' and downlinks >= downlink_limit:
                continue

            action = {'action': 'job', 'job_id': job['id']}
            candidates: list[tuple[tuple, str]] = []
            for sid in sorted(job['eligible_satellites']):
                if sid in reserved_satellites or not env.available(sid):
                    continue
                ok, _, _ = env.can_execute(sid, action)
                if not ok:
                    continue
                capacity = profiles[job['id']]['capacity'].get(sid, 0)
                # Prefer executors that do not hold the only current contact
                # for another, still unassigned job.
                energy_ratio = env.state[sid]['energy_wh'] / env.sats[sid]['capacity_wh']
                exclusive = sum(
                    other['id'] != job['id']
                    and other['id'] not in reserved_jobs
                    and profiles[other['id']]['current_windows'] == 1
                    and sid in other['eligible_satellites']
                    and env.s['environment'][sid][other['kind'] + '_available'][step]
                    for other in jobs
                )
                candidate_key = (exclusive, -capacity, -energy_ratio, sid)
                candidates.append((candidate_key, sid))

            if not candidates:
                continue
            _, sid = min(candidates)
            actions[sid] = action
            reserved_satellites.add(sid)
            reserved_jobs.add(job['id'])
            downlinks += int(job['kind'] == 'downlink')

        return actions

    def _profile(self, env: Any, job: dict[str, Any]) -> dict[str, Any]:
        capacity = {
            sid: self._project_capacity(env, sid, job)
            for sid in sorted(job['eligible_satellites'])
        }
        total = sum(capacity.values())
        # This is intentionally a capacity estimate, not a claim that all
        # eligible satellites can work simultaneously.
        return {
            'capacity': capacity,
            'capacity_total': total,
            'slack': total - job['remaining_steps'],
            'current_windows': sum(
                1 for sid in job['eligible_satellites']
                if env.available(sid)
                and env.s['environment'][sid][job['kind'] + '_available'][env.k]
            ),
        }

    def _project_capacity(self, env: Any, sid: str, job: dict[str, Any]) -> int:
        """Count locally executable slots until this job's deadline.

        A local state projection advances with this job only. Expired calibration
        consumes a slot when possible, and all other slots are idle unless the
        contact and local resource checks accept the job.  Thus the count
        includes known failures, current state, calibration and resource
        feasibility without predicting announced future events.
        """
        state = copy.deepcopy(env.state[sid])
        satellite = env.sats[sid]
        model = env.s['model']
        environment = env.s['environment'][sid]
        count = 0
        for step in range(env.k, job['deadline_step']):
            action = 'idle'
            if self._available_at(env, sid, step):
                if state['calibration_age_steps'] >= model['calibration_valid_steps']:
                    if self._local_can_execute(
                        satellite, model, environment, state, step, 'calibrate', job,
                        env.s['time']['step_s'],
                    ):
                        action = 'calibrate'
                elif self._local_can_execute(
                    satellite, model, environment, state, step, 'job', job,
                    env.s['time']['step_s'],
                ):
                    action = 'job'
                    count += 1

            payload = satellite['calibration_w'] if action == 'calibrate' else (
                satellite[job['kind'] + '_w'] if action == 'job' else 0.0
            )
            state['energy_wh'], state['temp_c'], _, _ = self._local_transition(
                satellite, model, environment, state, step, payload,
                env.s['time']['step_s'],
            )
            state['energy_wh'] = min(
                satellite['capacity_wh'], max(0.0, state['energy_wh']),
            )
            state['calibration_age_steps'] = 0 if action == 'calibrate' else state['calibration_age_steps'] + 1
        return count

    @staticmethod
    def _available_at(env: Any, sid: str, step: int) -> bool:
        return not any(
            failure['satellite_id'] == sid
            and failure['start_step'] <= step < failure['end_step']
            for failure in env.s['failures']
        )

    @staticmethod
    def _local_transition(
        satellite: dict[str, Any],
        model: dict[str, Any],
        environment: dict[str, Any],
        state: dict[str, float],
        step: int,
        payload: float,
        dt: int,
    ) -> tuple[float, float, float, float]:
        heater = satellite['heater_w'] if state['temp_c'] < model['heater_below_c'] else 0.0
        load = satellite['base_w'] + heater + payload
        delta = (environment['solar_w'][step] - load) * dt / 3600
        if delta >= 0:
            delta = delta * model['charge_efficiency'] if (
                model['charge_min_c'] <= state['temp_c'] <= model['charge_max_c']
            ) else 0.0
        else:
            delta /= model['discharge_efficiency']
        energy = state['energy_wh'] + delta
        equilibrium = environment['thermal_target_c'][step] + model['thermal_gain_c_per_w'] * load
        temp = equilibrium + (state['temp_c'] - equilibrium) * math.exp(
            -dt / model['thermal_tau_s']
        )
        return energy, temp, heater, load

    def _local_can_execute(
        self,
        satellite: dict[str, Any],
        model: dict[str, Any],
        environment: dict[str, Any],
        state: dict[str, float],
        step: int,
        action: str,
        job: dict[str, Any],
        dt: int,
    ) -> bool:
        if action == 'job' and not environment[job['kind'] + '_available'][step]:
            return False
        payload = satellite['calibration_w'] if action == 'calibrate' else satellite[job['kind'] + '_w']
        energy, temp, _, _ = self._local_transition(
            satellite, model, environment, state, step, payload, dt,
        )
        reserve = satellite['capacity_wh'] * model['reserve_soc_pct'] / 100
        return (
            state['energy_wh'] >= reserve - 1e-9
            and energy >= reserve - 1e-9
            and model['payload_min_c'] <= state['temp_c'] <= model['payload_max_c']
            and model['payload_min_c'] <= temp <= model['payload_max_c']
        )

    @staticmethod
    def _at_risk(job: dict[str, Any], profile: dict[str, Any]) -> bool:
        return profile['slack'] < 0 or (
            profile['slack'] == 0 and job['remaining_steps'] > 0
        )

    def _job_key(self, job: dict[str, Any], profile: dict[str, Any], step: int) -> tuple:
        risk = self._at_risk(job, profile)
        deadline = job['deadline_step'] - step
        if self.goal == 'priority':
            # Priority/value are separate tuple fields; value never dilutes
            # the critical-priority and deadline ordering.
            return (
                -(job['priority'] == 3),
                -job['priority'],
                -int(risk),
                profile['slack'],
                deadline,
                -profile['current_windows'],
                -job['value_usd'],
                -job['remaining_steps'],
                job['id'],
            )

        # Reject currently impossible completions before valuing work. Among
        # feasible jobs, protect urgent critical work; then maximize expected
        # on-time value, with contact slack only breaking value ties.
        value_density = job['value_usd'] / max(job['remaining_steps'], 1)
        return (
            int(profile['slack'] < 0),
            -int(job['priority'] == 3 and risk),
            -value_density,
            -job['value_usd'],
            profile['slack'],
            deadline,
            -profile['current_windows'],
            job['id'],
        )


def strategic_metadata(goal: str) -> dict[str, Any]:
    planner = StrategicPlanner(goal)
    return {
        'planner': STRATEGIC_VERSION,
        'algorithm': 'strategic',
        'goal': goal,
        'version': STRATEGIC_VERSION,
        'parameters': copy.deepcopy(planner.parameters),
    }


def run_strategic(
    scenario: dict,
    events: Iterable[dict[str, Any]] = (),
    goal: str = 'priority',
    run_metadata: dict[str, Any] | None = None,
) -> Session:
    """Execute the strategic policy using only events received at each step."""
    metadata = strategic_metadata(goal)
    if run_metadata:
        extras = copy.deepcopy(run_metadata)
        extras.pop('planner', None)
        extras.pop('algorithm', None)
        extras.pop('goal', None)
        extras.pop('version', None)
        extras.pop('parameters', None)
        metadata.update(extras)
    session = Session(scenario, metadata)
    event_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_map[event['at_step']].append(copy.deepcopy(event))

    planner = StrategicPlanner(goal)
    while session.env.k < scenario['time']['steps']:
        for event in event_map.get(session.env.k, []):
            session.apply_event(event)
        session.advance(planner.plan(session))
    return session
