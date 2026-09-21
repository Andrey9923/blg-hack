# Satellite Operations Planners

The model is kept in `model/`; deterministic policies are in `planner/`.
Run a scenario from the repository root without machine-specific paths:

```text
python -m planner.cli --scenario data/P01_intro.json --output results/p01-baseline.json
```

The same command works with `data/P02_shift.json`, `data/P03_energy.json`, or
`data/P04_demand.json`. Announced events can be included with
`--events examples/events_demo.json` when the event file belongs to that
scenario.

The feasibility baseline remains available with the command above. The
strategic planner is selected with `--planner strategic` and has two explicit
goals:

```text
python -m planner.cli --scenario data/P01_intro.json --planner strategic --goal priority --output results/p01-strategic.json
python -m planner.cli --scenario data/P01_intro.json --planner strategic --goal revenue --output results/p01-revenue.json
```

At every step strategic counts locally executable contact windows through each
job deadline. The count includes announced failures, current availability,
calibration prerequisites, energy, thermal limits, and the remaining work.
It then reserves a satellite and job before returning actions, and enforces
the downlink limit without changing the model's downlink rules. `priority`
lexicographically protects priority-3 work and deadlines before value and
resource tie-breaks. `revenue` protects work that is at risk of missing its
deadline, then selects the highest expected on-time value per remaining step;
urgent critical work remains ahead of non-urgent commercial work.

Compare both policies on the same scenario and event stream:

```text
python -m planner.cli --scenario data/P01_intro.json --compare --goal revenue --output results/p01-comparison.json
```

The comparison output contains both summaries and a machine-readable
strategic-minus-baseline `delta` for completion, revenue, deadlines, safety,
and blocked commands.

The baseline emits explicit idle commands for every satellite. At each step it
first calibrates satellites whose calibration age reached the validity limit.
It then sorts released work by critical/high priority, remaining deadline
slack, deadline, value, and job ID. For each selected job it checks the model's
energy, thermal, contact, availability, eligibility, and window rules, while
reserving each satellite and job once and enforcing the global downlink limit.
Both policies are deterministic and their exported commands can be checked
with `model.operations.replay_episode`.

For interactive execution, `PlannerRuntime` exposes only the current boundary.
Run to a boundary, submit events manually, and continue without providing a
future event queue to the planner:

```python
from model.resource_env import load
from planner.runtime import PlannerRuntime

run = PlannerRuntime(load('data/P02_shift.json'), planner='strategic', goal='priority')
run.run_until(72)                         # stopped before step 72
run.apply_event({                         # accepted only because step == 72
    'id': 'manual-job', 'at_step': 72, 'type': 'add_jobs',
    'jobs': [{
        'id': 'URGENT-MANUAL', 'kind': 'relay', 'release_step': 72,
        'deadline_step': 80, 'work_steps': 1,
        'eligible_satellites': ['S08'], 'priority': 3, 'value_usd': 25,
    }],
})
run.switch_goal('revenue')
branch = run.fork('revenue-branch', goal='revenue')
run.run()                                   # parent and branch are independent
branch.run()
branch.export('results/manual-branch.json')
replayed = branch.replay()
```

`run.result()` and `run.to_json()` contain only received events, executed
commands, history, and the current state; they do not contain a future plan.

Run the tests with:

```text
python -m unittest discover -s tests -v
```
