"""Run a compact boundary/event/fork/replay demonstration from the repo root."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model.resource_env import load  # noqa: E402
from planner.runtime import PlannerRuntime  # noqa: E402


DEMO_CONFIG = {
    "P01": {
        "path": ROOT / "data" / "P01_intro.json",
        "boundary": 12,
        "event": {
            "id": "demo-p01-outage",
            "at_step": 12,
            "type": "satellite_outage",
            "satellite_ids": ["S01"],
            "end_step": 18,
        },
    },
    "P02": {
        "path": ROOT / "data" / "P02_shift.json",
        "boundary": 72,
        "event": {
            "id": "demo-p02-outage",
            "at_step": 72,
            "type": "satellite_outage",
            "satellite_ids": ["S08", "S10"],
            "end_step": 90,
        },
    },
}


def run_demo(scenario_name: str) -> tuple[dict[str, PlannerRuntime], dict[str, Any]]:
    config = DEMO_CONFIG[scenario_name]
    scenario = load(config["path"])
    checkpoint = PlannerRuntime(
        scenario,
        planner="strategic",
        goal="priority",
        run_metadata={"scenario_id": scenario["meta"]["id"], "demo": True},
    )
    checkpoint.run_until(config["boundary"])
    checkpoint.apply_event(config["event"])

    branches = {
        goal: checkpoint.fork(f"demo-{goal}", goal=goal)
        for goal in ("priority", "revenue")
    }
    replay_verified = {}
    for goal, branch in branches.items():
        branch.run()
        replayed = branch.replay()
        replay_verified[goal] = (
            replayed.summary() == branch.summary()
            and replayed.env.trace == branch.session.env.trace
        )

    report = {
        "schema_version": "satellite-planner-demo-1.0",
        "scenario_id": scenario["meta"]["id"],
        "checkpoint_step": config["boundary"],
        "event": config["event"],
        "branch_point_state_digest": checkpoint.state_digest(),
        "branches": {goal: branch.summary() for goal, branch in branches.items()},
        "replay_verified": replay_verified,
    }
    return branches, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demonstrate stop, manual event, goal forks, export, and replay."
    )
    parser.add_argument(
        "--scenario", choices=tuple(DEMO_CONFIG), default="P01",
        help="P01 is compact; P02 demonstrates a full daily shift.",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Optional replayable result JSON for the selected export branch.",
    )
    parser.add_argument(
        "--export-goal", choices=("priority", "revenue"), default="revenue",
        help="Branch written by --output (default: revenue).",
    )
    args = parser.parse_args()

    branches, report = run_demo(args.scenario)
    event = report["event"]
    print("Satellite planner demo")
    print(f"Scenario: {report['scenario_id']}")
    print(f"Stopped before step {report['checkpoint_step']}")
    print(
        f"Manual event accepted: {event['type']} {event['satellite_ids']} "
        f"until step {event['end_step']}"
    )
    for goal in ("priority", "revenue"):
        summary = report["branches"][goal]
        print(
            f"{goal:8} | completed={summary['jobs_completed']} "
            f"critical={summary['critical_jobs_completed_on_time']} "
            f"missed={summary['jobs_due_missed']} "
            f"revenue={summary['revenue_usd']:.2f} "
            f"blocked={summary['blocked_command_count']}"
        )
    print("Replay verified: exact summary and trace for both branches")

    if args.output:
        payload = branches[args.export_goal].result()
        payload["demo_report"] = report
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        print(f"Replayable {args.export_goal} result: {args.output}")


if __name__ == "__main__":
    main()
