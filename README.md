# Satellite Operations Baseline

The model is kept in `model/`; the deterministic policy is in `planner/`.
Run a scenario from the repository root without machine-specific paths:

```text
python -m planner.cli --scenario data/P01_intro.json --output results/p01-baseline.json
```

The same command works with `data/P02_shift.json`, `data/P03_energy.json`, or
`data/P04_demand.json`. Announced events can be included with
`--events examples/events_demo.json` when the event file belongs to that
scenario.

The baseline emits explicit idle commands for every satellite. At each step it
first calibrates satellites whose calibration age reached the validity limit.
It then sorts released work by critical/high priority, remaining deadline
slack, deadline, value, and job ID. For each selected job it checks the model's
energy, thermal, contact, availability, eligibility, and window rules, while
reserving each satellite and job once and enforcing the global downlink limit.
This is a reproducible feasibility baseline, not an optimizer.

Run the tests with:

```text
python -m unittest discover -s tests -v
```
