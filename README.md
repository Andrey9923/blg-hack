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
It then reserves a satellite and job before returning actions, preserves a
satellite that is the only known future executor for another job, and chooses
among remaining executors using post-action SOC. The downlink limit is
enforced without changing the model's downlink rules. `priority`
lexicographically protects priority-3 work and deadlines before value and
resource tie-breaks. `revenue` protects work that is at risk of missing its
deadline, then selects the highest expected on-time value per remaining step;
urgent critical work remains ahead of non-urgent commercial work. These
parameters are recorded in strategic metadata (`strategic-v2`).

Compare both policies on the same scenario and event stream:

```text
python -m planner.cli --scenario data/P01_intro.json --compare --goal revenue --output results/p01-comparison.json
```

The comparison output contains both summaries and a machine-readable
strategic-minus-baseline `delta` for completion, revenue, deadlines, safety,
and blocked commands.

## Experiments and regressions

`planner.analysis` runs baseline and strategic on the same scenario and the
same events, but submits each event to `PlannerRuntime` only at its
`at_step`. It emits JSON with summaries, trace reason counters,
accepted/idle/rejected counters, unfinished jobs, resource indicators,
execution steps/time, and strategic-minus-baseline deltas:

```text
python -m planner.analysis --scenario data/P02_shift.json --goal priority
python -m planner.analysis --scenario data/P03_energy.json --goal revenue
python -m planner.analysis --scenario data/P04_demand.json --goal revenue --output results/p04-analysis.json
python -m planner.analysis --scenario data/P02_shift.json --events examples/events_demo.json --goal priority
```

The output distinguishes an actual rejected command from an intentional idle
row, a resource-related rejection, and a job unfinished after its deadline.
An unfinished job is a loss of the recorded greedy execution, not proof that
no other planner could complete it. The same boundary-driven flow is used for
the `events_demo` continuation.

The trade-off is visible in the report. Strategic improves throughput on P02
and P03 and substantially improves P04 revenue, while spending more CPU on
deadline lookahead. P01 is equal to baseline. On P04, `priority` improves
completed jobs and critical on-time work but reduces revenue and increases
below-reserve satellite steps; `revenue` improves commercial value at the cost
of critical completions. P03 `priority` also increases below-reserve steps.
These below-reserve states can arise during idle energy drain even though
commands are checked against the reserve before execution. The report tracks
blocked commands, brownout, reserve, and critical-SOC states and does not
interpret missed jobs as proven infeasibility.

The full regression suite includes exact replay, both goals, events-demo,
synthetic future-window protection, and an 8120-job performance guard:

```text
python -m unittest discover -s tests -v
```

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

## Operator web service

The operator service uses only the Python 3.10+ standard library. Start it from
the repository root:

```text
python -m web.app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` in a browser. The page can create a run, advance
it to a step boundary, accept one event at that boundary, change the goal,
fork an independent run, inspect summaries and trace explanations, and
download the current result. Invalid input is shown in the page and returned
as JSON with an HTTP 400 status; the server remains running.

The same flow can be driven without a browser. Every event is submitted only
when its `at_step` equals the run's current step; the service never consumes a
future event queue:

```text
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/scenarios
curl -X POST http://127.0.0.1:8000/api/runs -H "Content-Type: application/json" -d "{\"scenario_id\":\"P01_intro\",\"planner\":\"strategic\",\"goal\":\"priority\"}"
curl -X POST http://127.0.0.1:8000/api/runs/RUN_ID/advance -H "Content-Type: application/json" -d "{\"until_step\":1}"
curl -X POST http://127.0.0.1:8000/api/runs/RUN_ID/events -H "Content-Type: application/json" -d "{\"id\":\"operator-outage-1\",\"at_step\":1,\"type\":\"satellite_outage\",\"satellite_ids\":[\"S01\"],\"end_step\":3}"
curl -X POST http://127.0.0.1:8000/api/runs/RUN_ID/advance -H "Content-Type: application/json" -d "{\"steps\":1}"
curl -X POST http://127.0.0.1:8000/api/runs/RUN_ID/fork -H "Content-Type: application/json" -d "{\"branch_id\":\"revenue-branch\",\"goal\":\"revenue\"}"
curl "http://127.0.0.1:8000/api/runs/RUN_ID/explain?step=0&satellite_id=S01"
curl http://127.0.0.1:8000/api/runs/RUN_ID/result
```

`RUN_ID` is the ID returned by the create request. The API also accepts a
validated scenario object as `scenario` in the create payload. Built-in
scenarios are listed with their satellite, step, and job counts by
`GET /api/scenarios`. The main run endpoint returns the current observation,
summary, metadata, and available actions. `GET /api/runs/{id}/explain` reports
the recorded reason for a concrete trace row, including actual energy,
temperature, calibration, and job data; it does not turn a model decision into
a claim of objective impossibility.

Run state is intentionally in memory only. Restarting the process removes all
runs, events, branches, and results. The service limits the number of live
runs to 32, request JSON to 8 MiB, and custom scenarios to 6 MiB. It is a
local demonstration/operator surface, not a durable multi-user database or an
authentication layer.
