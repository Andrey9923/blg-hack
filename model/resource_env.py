from __future__ import annotations
import json, math, copy
from pathlib import Path

def finite(x):
    return isinstance(x, (int, float)) and (not isinstance(x, bool)) and math.isfinite(x)

def validate(s: dict) -> None:
    """Validate a complete operating scenario before changing any state."""
    def integer(x, name, minimum=0):
        if type(x) is not int or x < minimum:
            raise ValueError(f'{name}: expected an integer >= {minimum}')
    def identifier(x, name):
        if not isinstance(x, str) or not x.strip():
            raise ValueError(f'{name}: expected a nonempty string')
    if not isinstance(s, dict) or s.get('schema_version') != 'cosmo-B-ops-1.0':
        raise ValueError('Unsupported scenario schema')
    required_root = ('meta', 'time', 'satellites', 'model', 'environment', 'jobs', 'failures')
    if any(k not in s for k in required_root):
        raise ValueError('Missing scenario section')
    if not isinstance(s['meta'], dict):
        raise ValueError('meta must be an object')
    identifier(s['meta'].get('id'), 'meta.id')
    identifier(s['meta'].get('title'), 'meta.title')
    if not isinstance(s['time'], dict):
        raise ValueError('time must be an object')
    n, dt = s['time'].get('steps'), s['time'].get('step_s')
    integer(n, 'time.steps', 1)
    if n > 288 or type(dt) is not int or dt != 300:
        raise ValueError('Time grid: 1..288 steps, 300 seconds per step')
    sats = s['satellites']
    if not isinstance(sats, list) or not 1 <= len(sats) <= 48:
        raise ValueError('satellites must contain 1..48 objects')
    ids = []
    fields = ('capacity_wh', 'initial_soc_pct', 'initial_temp_c', 'base_w',
              'heater_w', 'calibration_w', 'downlink_w', 'relay_w',
              'initial_calibration_age_steps')
    for v in sats:
        if not isinstance(v, dict):
            raise ValueError('Satellite must be an object')
        identifier(v.get('id'), 'satellite.id')
        ids.append(v['id'])
        if not all(finite(v.get(k)) for k in fields):
            raise ValueError('Missing or non-finite satellite field')
        integer(v['initial_calibration_age_steps'], 'initial_calibration_age_steps')
        if v['capacity_wh'] <= 0 or not 0 <= v['initial_soc_pct'] <= 100:
            raise ValueError('Invalid battery capacity or initial charge')
        if any(v[k] < 0 for k in ('base_w', 'heater_w', 'calibration_w', 'downlink_w', 'relay_w')):
            raise ValueError('Negative power')
    if len(ids) != len(set(ids)):
        raise ValueError('Satellite IDs must be unique')
    if not isinstance(s['environment'], dict) or set(s['environment']) != set(ids):
        raise ValueError('Environment keys must match satellite IDs')
    for sid in ids:
        env = s['environment'][sid]
        if not isinstance(env, dict):
            raise ValueError('Satellite environment must be an object')
        for key in ('solar_w', 'thermal_target_c', 'downlink_available', 'relay_available'):
            if not isinstance(env.get(key), list) or len(env[key]) != n:
                raise ValueError(f'{sid}.{key}: expected {n} values')
        if any(not finite(x) or x < 0 for x in env['solar_w']):
            raise ValueError('Invalid solar power')
        if any(not finite(x) for x in env['thermal_target_c']):
            raise ValueError('Invalid thermal target')
        for key in ('downlink_available', 'relay_available'):
            if any(type(x) is not bool for x in env[key]):
                raise ValueError('Availability must contain booleans')
    m = s['model']
    fields = ('reserve_soc_pct', 'critical_soc_pct', 'charge_efficiency',
              'discharge_efficiency', 'thermal_tau_s', 'thermal_gain_c_per_w',
              'heater_below_c', 'payload_min_c', 'payload_max_c', 'charge_min_c',
              'charge_max_c', 'calibration_valid_steps', 'downlink_parallel_limit')
    if not isinstance(m, dict) or not all(finite(m.get(k)) for k in fields):
        raise ValueError('Missing or non-finite model value')
    integer(m['calibration_valid_steps'], 'calibration_valid_steps', 1)
    integer(m['downlink_parallel_limit'], 'downlink_parallel_limit', 1)
    if not (0 <= m['critical_soc_pct'] < m['reserve_soc_pct'] < 100
            and 0 < m['charge_efficiency'] <= 1
            and 0 < m['discharge_efficiency'] <= 1
            and m['thermal_tau_s'] > 0 and m['thermal_gain_c_per_w'] >= 0
            and m['payload_min_c'] <= m['payload_max_c']
            and m['charge_min_c'] <= m['charge_max_c']):
        raise ValueError('Invalid model parameters')
    jobs = s['jobs']
    if not isinstance(jobs, list):
        raise ValueError('jobs must be a list')
    seen = set()
    for j in jobs:
        if not isinstance(j, dict):
            raise ValueError('Job must be an object')
        identifier(j.get('id'), 'job.id')
        if j['id'] in seen:
            raise ValueError('Duplicate job ID')
        seen.add(j['id'])
        if j.get('kind') not in ('downlink', 'relay'):
            raise ValueError('Invalid job kind')
        for k in ('release_step', 'deadline_step', 'work_steps', 'priority'):
            integer(j.get(k), k)
        if not (0 <= j['release_step'] < j['deadline_step'] <= n
                and 0 < j['work_steps'] <= j['deadline_step'] - j['release_step']
                and 1 <= j['priority'] <= 3 and finite(j.get('value_usd'))
                and j['value_usd'] >= 0):
            raise ValueError('Invalid job bounds')
        eligible = j.get('eligible_satellites')
        if (not isinstance(eligible, list) or not eligible
                or not all(isinstance(x, str) for x in eligible)
                or len(eligible) != len(set(eligible)) or not set(eligible) <= set(ids)):
            raise ValueError('Invalid job eligibility')
        if j['kind'] == 'downlink' and len(eligible) != 1:
            raise ValueError('A downlink job has exactly one eligible satellite')
    if not isinstance(s['failures'], list):
        raise ValueError('failures must be a list')
    for f in s['failures']:
        if not isinstance(f, dict) or f.get('satellite_id') not in ids:
            raise ValueError('Invalid failure satellite')
        integer(f.get('start_step'), 'start_step')
        integer(f.get('end_step'), 'end_step')
        if not 0 <= f['start_step'] < f['end_step'] <= n:
            raise ValueError('Invalid failure interval')

def load(path: str | Path) -> dict:
    s = json.loads(Path(path).read_text(encoding='utf-8'))
    validate(s)
    return s

class Environment:

    def __init__(self, scenario: dict):
        validate(scenario)
        self.s = copy.deepcopy(scenario)
        self.k = 0
        self.sats = {v['id']: v for v in self.s['satellites']}
        self.state = {i: {'energy_wh': v['capacity_wh'] * v['initial_soc_pct'] / 100, 'temp_c': v['initial_temp_c'], 'calibration_age_steps': v['initial_calibration_age_steps']} for i, v in self.sats.items()}
        self.jobs = {j['id']: dict(j, remaining_steps=j['work_steps'], completed_step=None) for j in self.s['jobs']}
        self.trace = []
        self.completed = []

    def available(self, sid: str) -> bool:
        return not any((f['satellite_id'] == sid and f['start_step'] <= self.k < f['end_step'] for f in self.s['failures']))

    def observation(self) -> dict:
        return {'step': self.k, 'state': copy.deepcopy(self.state), 'jobs': copy.deepcopy(self.jobs), 'available': {sid: self.available(sid) for sid in self.sats}}

    def transition(self, sid: str, payload_w: float) -> tuple[float, float, float, float]:
        v = self.sats[sid]
        st = self.state[sid]
        m = self.s['model']
        e = self.s['environment'][sid]
        dt = self.s['time']['step_s']
        heater = v['heater_w'] if st['temp_c'] < m['heater_below_c'] else 0.0
        load_w = v['base_w'] + heater + payload_w
        solar = e['solar_w'][self.k]
        delta = (solar - load_w) * dt / 3600
        if delta >= 0:
            delta = delta * m['charge_efficiency'] if m['charge_min_c'] <= st['temp_c'] <= m['charge_max_c'] else 0.0
        else:
            delta /= m['discharge_efficiency']
        raw_energy = st['energy_wh'] + delta
        equilibrium = e['thermal_target_c'][self.k] + m['thermal_gain_c_per_w'] * load_w
        temp = equilibrium + (st['temp_c'] - equilibrium) * math.exp(-dt / m['thermal_tau_s'])
        return (raw_energy, temp, heater, load_w)

    def can_execute(self, sid: str, action: dict) -> tuple[bool, str, float]:
        if sid not in self.sats:
            return (False, 'unknown_satellite', 0)
        v = self.sats[sid]
        st = self.state[sid]
        m = self.s['model']
        kind = action.get('action', 'idle')
        if kind == 'idle':
            return (True, 'idle', 0)
        if not self.available(sid):
            return (False, 'satellite_unavailable', 0)
        if kind == 'calibrate':
            power = v['calibration_w']
        elif kind == 'job':
            j = self.jobs.get(action.get('job_id'))
            if j is None:
                return (False, 'unknown_job', 0)
            if j['completed_step'] is not None or j['remaining_steps'] <= 0:
                return (False, 'already_completed', 0)
            if not j['release_step'] <= self.k < j['deadline_step']:
                return (False, 'outside_job_window', 0)
            if sid not in j['eligible_satellites']:
                return (False, 'ineligible_satellite', 0)
            if not self.s['environment'][sid][j['kind'] + '_available'][self.k]:
                return (False, 'no_contact', 0)
            if st['calibration_age_steps'] >= m['calibration_valid_steps']:
                return (False, 'calibration_required', 0)
            power = v[j['kind'] + '_w']
        else:
            return (False, 'unknown_action', 0)
        en, temp, _, _ = self.transition(sid, power)
        if st['energy_wh'] < v['capacity_wh'] * m['reserve_soc_pct'] / 100 - 1e-09 or en < v['capacity_wh'] * m['reserve_soc_pct'] / 100 - 1e-09:
            return (False, 'energy_reserve', 0)
        if not (m['payload_min_c'] <= st['temp_c'] <= m['payload_max_c'] and m['payload_min_c'] <= temp <= m['payload_max_c']):
            return (False, 'thermal_limit', 0)
        return (True, 'accepted', power)

    def step(self, actions: dict[str, dict]) -> list[dict]:
        if self.k >= self.s['time']['steps']:
            raise ValueError('Simulation is finished')
        if set(actions) - set(self.sats):
            raise ValueError('Unknown satellite in commands')
        used_jobs = set()
        downlinks = 0
        rows = []
        for sid in sorted(self.sats):
            requested = actions.get(sid, {'action': 'idle'})
            ok, reason, power = self.can_execute(sid, requested)
            j = self.jobs.get(requested.get('job_id')) if requested.get('action') == 'job' else None
            if ok and j:
                if j['id'] in used_jobs:
                    ok, reason, power = (False, 'duplicate_job_in_step', 0)
                elif j['kind'] == 'downlink' and downlinks >= self.s['model']['downlink_parallel_limit']:
                    ok, reason, power = (False, 'ground_capacity', 0)
            actual = requested.get('action', 'idle') if ok else 'idle'
            st = self.state[sid]
            before = dict(st)
            en, temp, heater, load_w = self.transition(sid, power)
            st['energy_wh'] = min(self.sats[sid]['capacity_wh'], max(0.0, en))
            st['temp_c'] = temp
            st['calibration_age_steps'] = 0 if actual == 'calibrate' else st['calibration_age_steps'] + 1
            completed = None
            if ok and actual == 'job':
                used_jobs.add(j['id'])
                downlinks += int(j['kind'] == 'downlink')
                j['remaining_steps'] -= 1
                if j['remaining_steps'] == 0:
                    j['completed_step'] = self.k + 1
                    completed = j['id']
                    self.completed.append(j['id'])
            rows.append({'step': self.k, 'satellite_id': sid, 'requested': requested, 'executed': actual, 'reason': reason, 'energy_before_wh': round(before['energy_wh'], 6), 'energy_after_wh': round(st['energy_wh'], 6), 'temp_before_c': round(before['temp_c'], 6), 'temp_after_c': round(temp, 6), 'solar_w': self.s['environment'][sid]['solar_w'][self.k], 'heater_w': heater, 'load_w': load_w, 'calibration_age_steps': st['calibration_age_steps'], 'completed_job': completed, 'brownout': en < 0, 'below_reserve': st['energy_wh'] < self.sats[sid]['capacity_wh'] * self.s['model']['reserve_soc_pct'] / 100 - 1e-09})
        self.trace.extend(rows)
        self.k += 1
        return rows

    def summary(self) -> dict:
        finished = [j for j in self.jobs.values() if j['completed_step'] is not None]
        due = [j for j in self.jobs.values() if j['deadline_step'] <= self.k]
        critical = [j for j in due if j['priority'] == 3]
        return {
            'steps_executed': self.k,
            'jobs_total': len(self.jobs),
            'jobs_completed': len(finished),
            'jobs_due': len(due),
            'jobs_due_missed': sum(j['completed_step'] is None for j in due),
            'critical_jobs_due': len(critical),
            'critical_jobs_completed_on_time': sum(j['completed_step'] is not None for j in critical),
            'revenue_usd': round(sum(j['value_usd'] for j in finished), 6),
            'blocked_command_count': sum(r['reason'] not in ('accepted', 'idle') for r in self.trace),
            'below_reserve_satellite_steps': sum(r['below_reserve'] for r in self.trace),
            'brownout_satellite_steps': sum(r['brownout'] for r in self.trace),
            'critical_soc_satellite_steps': sum(
                100 * r['energy_after_wh'] / self.sats[r['satellite_id']]['capacity_wh']
                < self.s['model']['critical_soc_pct'] for r in self.trace),
            'minimum_soc_pct': round(min(
                min(v['initial_soc_pct'] for v in self.sats.values()),
                min((100 * r['energy_after_wh'] / self.sats[r['satellite_id']]['capacity_wh']
                     for r in self.trace), default=100.0)), 6)
        }

