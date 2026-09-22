from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
import json
import math
from collections import Counter
import re
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from model.resource_env import load, validate
from model.operations import digest, replay_episode
from planner.runtime import PlannerRuntime


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "data"
SCENARIO_FILES = {
    "P01_intro": "P01_intro.json",
    "P02_shift": "P02_shift.json",
    "P03_energy": "P03_energy.json",
    "P04_demand": "P04_demand.json",
}
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_SCENARIO_BYTES = 6 * 1024 * 1024
MAX_RUNS = 32
ID_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def error_payload(message: str, *, code: str = "bad_request") -> dict[str, str]:
    return {"error": code, "message": message}


class RunStore:
    """Thread-safe in-memory ownership of independent PlannerRuntime objects."""

    def __init__(self) -> None:
        self._scenarios = self._load_scenarios()
        self._runs: dict[str, PlannerRuntime] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._lock = threading.RLock()

    def list_runs(self):
        with self._lock:
            return [{"run_id": key, "goal": run.goal, "step": run.current_step}
                    for key, run in self._runs.items()]

    @staticmethod
    def _load_scenarios() -> dict[str, dict]:
        scenarios = {}
        for scenario_id, filename in SCENARIO_FILES.items():
            scenarios[scenario_id] = load(SCENARIO_DIR / filename)
        return scenarios

    def scenario_list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": scenario_id,
                "title": scenario["meta"]["title"],
                "satellites": len(scenario["satellites"]),
                "steps": scenario["time"]["steps"],
                "jobs": len(scenario["jobs"]),
            }
            for scenario_id, scenario in self._scenarios.items()
        ]

    @staticmethod
    def _apply_settings(scenario: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """Apply the small set of operator-facing experiment controls atomically."""
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        if set(settings) - {"satellite_id", "initial_soc_pct", "solar_multiplier", "job_id", "priority", "outage"}:
            raise ValueError("Unknown experiment setting")
        configured = copy.deepcopy(scenario)
        satellites = {item["id"]: item for item in configured["satellites"]}
        satellite_id = settings.get("satellite_id")
        if satellite_id is not None and (not isinstance(satellite_id, str) or satellite_id not in satellites):
            raise ValueError("settings.satellite_id refers to an unknown satellite")

        if "initial_soc_pct" in settings:
            if satellite_id is None or type(settings["initial_soc_pct"]) not in (int, float):
                raise ValueError("initial_soc_pct requires satellite_id and a number")
            if not 0 <= settings["initial_soc_pct"] <= 100:
                raise ValueError("initial_soc_pct must be between 0 and 100")
            satellites[satellite_id]["initial_soc_pct"] = settings["initial_soc_pct"]

        if "solar_multiplier" in settings:
            multiplier = settings["solar_multiplier"]
            if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier < 0:
                raise ValueError("solar_multiplier must be a finite nonnegative number")
            for sid in ([satellite_id] if satellite_id else satellites):
                configured["environment"][sid]["solar_w"] = [
                    value * multiplier for value in configured["environment"][sid]["solar_w"]
                ]

        if "job_id" in settings or "priority" in settings:
            job_id = settings.get("job_id")
            priority = settings.get("priority")
            jobs = {job["id"]: job for job in configured["jobs"]}
            if not isinstance(job_id, str) or job_id not in jobs:
                raise ValueError("settings.job_id refers to an unknown job")
            if type(priority) is not int or priority not in (1, 2, 3):
                raise ValueError("settings.priority must be 1, 2 or 3")
            jobs[job_id]["priority"] = priority

        outage = settings.get("outage")
        if outage is not None:
            if not isinstance(outage, dict):
                raise ValueError("settings.outage must be an object")
            ids = outage.get("satellite_ids")
            start = outage.get("start_step")
            end = outage.get("end_step")
            total = configured["time"]["steps"]
            if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids)
                    or len(ids) != len(set(ids))
                    or not all(item in satellites for item in ids)):
                raise ValueError("outage satellite_ids must contain known unique satellites")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= total:
                raise ValueError("outage interval must fit inside the scenario")
            configured["failures"].extend(
                {"satellite_id": item, "start_step": start, "end_step": end}
                for item in ids
            )
        validate(configured)
        if configured != scenario:
            configured["meta"] = {
                **configured["meta"],
                "id": f"experiment-{digest(configured)[:12]}",
                "title": f"{scenario['meta']['title']} · эксперимент",
            }
        return configured

    def prepare_scenario(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        if "scenario" in payload:
            scenario = copy.deepcopy(payload["scenario"])
            if len(json_bytes(scenario)) > MAX_SCENARIO_BYTES:
                raise ValueError("scenario exceeds the 6 MiB size limit")
        else:
            scenario_id = payload.get("scenario_id")
            if not isinstance(scenario_id, str) or scenario_id not in self._scenarios:
                raise ValueError("Unknown built-in scenario_id")
            scenario = copy.deepcopy(self._scenarios[scenario_id])
        validate(scenario)
        if "settings" in payload:
            scenario = self._apply_settings(scenario, payload["settings"])
        return scenario

    def create(self, payload: dict[str, Any]) -> PlannerRuntime:
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        scenario = self.prepare_scenario(payload)
        scenario_id = scenario["meta"]["id"]
        planner = payload.get("planner", "strategic")
        goal = payload.get("goal", "priority")
        if not isinstance(planner, str) or planner not in ("baseline", "strategic"):
            raise ValueError("planner must be 'baseline' or 'strategic'")
        if not isinstance(goal, str) or goal not in ("priority", "revenue"):
            raise ValueError("goal must be 'priority' or 'revenue'")
        validate(scenario)
        with self._lock:
            if len(self._runs) >= MAX_RUNS:
                raise ValueError("in-memory run limit reached; restart the server to clear runs")
            run_id = uuid.uuid4().hex
            run = PlannerRuntime(
                scenario,
                planner=planner,
                goal=goal,
                run_metadata={"run_id": run_id, "scenario_id": scenario_id},
            )
            self._runs[run_id] = run
            self._locks[run_id] = threading.RLock()
        return run

    def get(self, run_id: str) -> tuple[PlannerRuntime, threading.RLock]:
        with self._lock:
            run = self._runs.get(run_id)
            lock = self._locks.get(run_id)
        if run is None or lock is None:
            raise KeyError("run not found")
        return run, lock

    def add_fork(self, parent_id: str, branch: PlannerRuntime) -> None:
        self.add_forks([branch])

    def add_forks(self, branches: list[PlannerRuntime]) -> None:
        with self._lock:
            if len(self._runs) + len(branches) > MAX_RUNS:
                raise ValueError("in-memory run limit reached; restart the server to clear runs")
            ids = [branch.run_metadata.get("run_id") for branch in branches]
            if (any(not isinstance(run_id, str) or run_id in self._runs for run_id in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError("fork generated a duplicate run id")
            for run_id, branch in zip(ids, branches):
                self._runs[run_id] = branch
                self._locks[run_id] = threading.RLock()


def available_actions(run: PlannerRuntime) -> dict[str, Any]:
    total = run.env.s["time"]["steps"]
    return {
        "advance": run.current_step < total,
        "event": run.current_step < total,
        "goal": ["priority", "revenue"],
        "fork": True,
        "result": True,
        "explain": bool(run.session.env.trace),
        "current_step": run.current_step,
        "scenario_steps": total,
    }


def run_view(run: PlannerRuntime) -> dict[str, Any]:
    trace = run.session.env.trace
    telemetry: dict[str, list[dict[str, Any]]] = {}
    for row in trace:
        telemetry.setdefault(row["satellite_id"], []).append({
            "step": row["step"],
            "satellite_id": row["satellite_id"],
            "energy_wh": row["energy_after_wh"],
            "temp_c": row["temp_after_c"],
            "executed": row["executed"],
            "reason": row["reason"],
            "requested": copy.deepcopy(row["requested"]),
            "job_id": row.get("requested", {}).get("job_id") if isinstance(row.get("requested"), dict) else None,
        })
    return {
        "run_id": run.run_metadata.get("run_id"),
        "revision": digest(run.result()),
        "observation": run.observation(),
        "summary": run.summary(),
        "metadata": copy.deepcopy(run.run_metadata),
        "history": [
            {**copy.deepcopy({key: value for key, value in entry.items() if key not in ("commands", "trace")}),
             **({"command_count": len(entry["commands"])} if "commands" in entry else {})}
            for entry in run.history
        ],
        "available_actions": available_actions(run),
        "telemetry": telemetry,
        "trace_count": len(trace),
        "satellite_specs": copy.deepcopy(run.env.sats),
        "model": copy.deepcopy(run.env.s["model"]),
        "time": copy.deepcopy(run.env.s["time"]),
        "scenario_title": run.initial_scenario["meta"]["title"],
        "events": copy.deepcopy(run.events),
        "utilization": {
            sid: (sum(row["executed"] == "job" for row in rows) / run.current_step
                  if run.current_step else None)
            for sid in run.env.sats
            for rows in [telemetry.get(sid, [])]
        },
    }


def explain_trace(run: PlannerRuntime, step: int, satellite_id: str) -> dict[str, Any]:
    if type(step) is not int or step < 0:
        raise ValueError("step must be an integer >= 0")
    if not isinstance(satellite_id, str) or not satellite_id:
        raise ValueError("satellite_id is required")
    row = next(
        (item for item in run.env.trace
         if item.get("step") == step and item.get("satellite_id") == satellite_id),
        None,
    )
    if row is None:
        raise ValueError("no trace record exists for that step and satellite")
    requested = row.get("requested", {})
    job_id = requested.get("job_id") if isinstance(requested, dict) else None
    job = copy.deepcopy(run.env.jobs.get(job_id)) if job_id else None
    reason = row.get("reason")
    if reason == "idle":
        outcome_category = "idle"
    elif reason == "accepted":
        outcome_category = "executed"
    elif reason in {"energy_reserve", "thermal_limit", "satellite_unavailable",
                    "no_contact", "ground_capacity"}:
        outcome_category = "resource_deficit"
    else:
        outcome_category = "command_rejected"
    deadline_missed = bool(
        job and job.get("completed_step") is None
        and job.get("deadline_step", run.current_step + 1) <= run.current_step
    )
    descriptions = {
        "idle": "No payload was requested for this satellite at this step.",
        "accepted": "The requested operation passed the model checks and was executed.",
        "energy_reserve": "The requested operation was not executed because the reserve-energy check failed at this step.",
        "no_contact": "The requested job was not executed because the required contact was unavailable at this step.",
        "calibration_required": "The requested job was not executed because calibration age reached the configured validity limit.",
        "satellite_unavailable": "The requested operation was not executed because the satellite was unavailable at this step.",
        "thermal_limit": "The requested operation was not executed because the current or projected temperature was outside the payload range.",
        "outside_job_window": "The requested job was not executed because this step is outside its release/deadline window.",
        "ineligible_satellite": "The requested job was not executed because this satellite is not eligible for it.",
        "ground_capacity": "The requested downlink was not executed because the per-step ground capacity was already used.",
        "duplicate_job_in_step": "The requested job was not executed because another satellite already used it in this step.",
    }
    # Reconstruct the information available before this specific decision, not
    # today's environment (which may include later outages or added jobs).
    checkpoint = replay_episode(
        run.initial_scenario,
        [event for event in run.events if event["at_step"] <= step],
        [command for command in run.commands if command["step"] < step],
        step,
    )
    env = checkpoint.env
    candidates = []
    for candidate in env.jobs.values():
        if (satellite_id in candidate["eligible_satellites"]
                and candidate["completed_step"] is None
                and candidate["release_step"] <= step < candidate["deadline_step"]):
            allowed, candidate_reason, _ = env.can_execute(
                satellite_id, {"action": "job", "job_id": candidate["id"]})
            candidates.append({"id": candidate["id"], "reason": candidate_reason,
                               "locally_allowed": allowed, "priority": candidate["priority"],
                               "remaining_steps": candidate["remaining_steps"],
                               "deadline_step": candidate["deadline_step"]})
    candidate_counts = dict(Counter(candidate["reason"] for candidate in candidates))
    if not env.available(satellite_id):
        decision = "Аппарат недоступен на этом шаге по полученному к этому моменту интервалу отказа."
    elif row["executed"] == "calibrate":
        decision = "Планировщик выполнил обслуживание: истёк срок калибровки, без неё полезная работа не допускается."
    elif reason == "idle":
        if not candidates:
            decision = "На этом шаге нет открытых незавершённых заданий с этим допустимым исполнителем."
        elif not any(candidate["locally_allowed"] for candidate in candidates):
            decision = "Доступные задания не прошли локальные проверки. Причины перечислены ниже."
        else:
            decision = "Есть локально допустимая работа. Назначения ограничены общей ёмкостью downlink и занятостью заданий другими аппаратами; это выбор планировщика, а не доказательство невозможности."
    elif reason == "accepted":
        decision = "Назначенная операция прошла проверки модели и общих ресурсов. Порядок выбора заданий задаётся целью и версией планировщика."
    else:
        decision = "Модель отклонила запрос и выполнила ожидание. Причина отказа относится только к этой операции на этом шаге."
    return {
        "run_id": run.run_metadata.get("run_id"),
        "step": step,
        "satellite_id": satellite_id,
        "requested": copy.deepcopy(row.get("requested")),
        "executed": row.get("executed"),
        "reason": reason,
        "outcome_category": outcome_category,
        "deadline_missed": deadline_missed,
        "explanation": descriptions.get(reason, "The operation was evaluated by the runtime at this step."),
        "record": copy.deepcopy(row),
        "energy": {
            "before_wh": row.get("energy_before_wh"),
            "after_wh": row.get("energy_after_wh"),
            "below_reserve": row.get("below_reserve"),
        },
        "temperature": {
            "before_c": row.get("temp_before_c"),
            "after_c": row.get("temp_after_c"),
        },
        "calibration": {"age_steps": row.get("calibration_age_steps")},
        "job": job,
        "decision": decision,
        "known_at_step": {
            "events": copy.deepcopy(checkpoint.events),
            "state": copy.deepcopy(env.state[satellite_id]),
            "available": env.available(satellite_id),
            "downlink_available": env.s["environment"][satellite_id]["downlink_available"][step],
            "relay_available": env.s["environment"][satellite_id]["relay_available"][step],
            "candidate_counts": candidate_counts,
            "candidates": candidates,
        },
        "note": "This is an explanation of the recorded model decision at this step, not a proof of objective impossibility.",
    }


class OperatorService:
    """HTTP-independent API operations, useful for tests and embedding."""

    def __init__(self, store: RunStore | None = None) -> None:
        self.store = store or RunStore()

    def get_run(self, run_id: str) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        with lock:
            return run_view(run)

    def advance(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        if ("steps" in payload) == ("until_step" in payload):
            raise ValueError("provide exactly one of steps or until_step")
        with lock:
            if "steps" in payload:
                run.run_steps(payload["steps"])
            else:
                run.run_until(payload["until_step"])
            return run_view(run)

    def event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        event = payload.get("event") if isinstance(payload, dict) and "event" in payload else payload
        with lock:
            before = run.state_digest()
            run.apply_event(event)
            result = run_view(run)
            result["event_accepted"] = True
            result["state_digest_before"] = before
            return result

    def goal(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("goal"), str):
            raise ValueError("goal is required")
        with lock:
            run.switch_goal(payload["goal"])
            return run_view(run)

    def fork(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        parent, parent_lock = self.store.get(run_id)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        with parent_lock:
            branch_id = payload.get("branch_id")
            if branch_id is not None and (not isinstance(branch_id, str) or not ID_RE.fullmatch(branch_id)):
                raise ValueError("branch_id contains unsupported characters")
            if not branch_id:
                branch_id = "branch-" + uuid.uuid4().hex[:10]
            child = parent.fork(branch_id, planner=payload.get("planner"), goal=payload.get("goal"))
            child.run_metadata["source_revision"] = digest(parent.result())
            self.store.add_fork(run_id, child)
            return run_view(child)

    def compare(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Finish two independent continuations from the same actual checkpoint."""
        parent, parent_lock = self.store.get(run_id)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        first_goal = payload.get("first_goal", "priority")
        second_goal = payload.get("second_goal", "revenue")
        if first_goal not in ("priority", "revenue") or second_goal not in ("priority", "revenue"):
            raise ValueError("comparison goals must be 'priority' or 'revenue'")
        if first_goal == second_goal:
            raise ValueError("comparison goals must be different")
        with parent_lock:
            target = payload.get("until_step", parent.env.s["time"]["steps"])
            if type(target) is not int or not parent.current_step <= target <= parent.env.s["time"]["steps"]:
                raise ValueError("until_step must be between the checkpoint and the end of the scenario")
            checkpoint = {"step": parent.current_step, "state_digest": parent.state_digest()}
            base = f"comparison-{uuid.uuid4().hex[:8]}"
            first = parent.fork(f"{base}-a", goal=first_goal)
            second = parent.fork(f"{base}-b", goal=second_goal)
            for branch in (first, second):
                branch.run_metadata["source_revision"] = digest(parent.result())
            self.store.add_forks([first, second])
        first_run, first_lock = self.store.get(first.run_metadata["run_id"])
        second_run, second_lock = self.store.get(second.run_metadata["run_id"])
        with first_lock:
            first_run.run_until(target)
            first_view = run_view(first_run)
        with second_lock:
            second_run.run_until(target)
            second_view = run_view(second_run)
        left = first_view["summary"]
        right = second_view["summary"]
        metrics = ("jobs_completed", "jobs_due_missed", "critical_jobs_completed_on_time", "revenue_usd", "minimum_soc_pct")
        return {
            "source_run_id": run_id,
            "checkpoint": checkpoint,
            "branches": [first_view, second_view],
            "delta": {key: round(right[key] - left[key], 6) for key in metrics},
            "interpretation": "Сравнение выполнено на одном состоянии и одном полученном префиксе событий.",
        }

    def adopt(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        branch_id = payload.get("branch_id")
        if not isinstance(branch_id, str) or branch_id == run_id:
            raise ValueError("branch_id must identify a child run")
        parent, parent_lock = self.store.get(run_id)
        child, child_lock = self.store.get(branch_id)
        with parent_lock, child_lock:
            if child.run_metadata.get("parent_run_id") != run_id:
                raise ValueError("Selected run is not a direct child")
            if child.run_metadata.get("source_revision") != digest(parent.result()):
                raise BranchConflict("Parent changed since the fork; create a new comparison")
            replacement = copy.deepcopy(child)
            for key in ("parent_run_id", "parent_branch_point", "parent_branch_point_step",
                        "parent_branch_point_digest", "branch_id", "source_revision"):
                replacement.run_metadata.pop(key, None)
                if key in parent.run_metadata:
                    replacement.run_metadata[key] = copy.deepcopy(parent.run_metadata[key])
            replacement.run_metadata["run_id"] = run_id
            replacement.run_metadata["selected_branch_id"] = branch_id
            replacement.history.append({"type": "branch_selected", "step": replacement.current_step,
                                        "branch_id": branch_id})
            # Preserve object identity for requests already waiting on this lock.
            parent.__dict__.clear()
            parent.__dict__.update(replacement.__dict__)
            return run_view(parent)

    def explain(self, run_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        try:
            step = int(query.get("step", [""])[0])
        except (TypeError, ValueError):
            raise ValueError("step query parameter must be an integer")
        satellite_id = query.get("satellite_id", [""])[0]
        with lock:
            return explain_trace(run, step, satellite_id)


class BranchConflict(ValueError):
    pass


class OperatorHandler(BaseHTTPRequestHandler):
    server_version = "PlannerOperator/1.0"

    @property
    def service(self) -> OperatorService:
        return self.server.service  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        if getattr(self, "_buffer_response", False):
            self._pending_response = (status, payload, content_type)
            return
        body = payload if isinstance(payload, bytes) else json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError:
            raise ValueError("Content-Length must be an integer")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request JSON exceeds the 8 MiB size limit")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON: {exc}")
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if method == "GET" and path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok", "service": "planner-runtime"})
            return
        if method == "GET" and path == "/":
            html_path = Path(__file__).with_name("index.html")
            self._send(HTTPStatus.OK, html_path.read_bytes(), "text/html; charset=utf-8")
            return
        if method == "GET" and path == "/api/scenarios":
            self._send(HTTPStatus.OK, {"scenarios": self.service.store.scenario_list()})
            return
        if method == "POST" and path == "/api/scenarios/prepare":
            self._send(HTTPStatus.OK, {"scenario": self.service.store.prepare_scenario(self._read_json())})
            return
        if path == "/api/runs" and method == "GET":
            self._send(HTTPStatus.OK, {"runs": self.service.store.list_runs()})
            return
        if path == "/api/runs" and method == "POST":
            run = self.service.store.create(self._read_json())
            self._send(HTTPStatus.CREATED, run_view(run))
            return
        prefix = "/api/runs/"
        if not path.startswith(prefix):
            self._send(HTTPStatus.NOT_FOUND, error_payload("endpoint not found", code="not_found"))
            return
        rest = unquote(path[len(prefix):]).rstrip("/")
        suffixes = {
            "/advance": "advance",
            "/events": "event",
            "/goal": "goal",
            "/fork": "fork",
            "/compare": "compare",
            "/adopt": "adopt",
            "/result": "result",
            "/explain": "explain",
        }
        operation = None
        run_id = rest
        for suffix, name in suffixes.items():
            if rest.endswith(suffix):
                operation = name
                run_id = rest[:-len(suffix)].rstrip("/")
                break
        if not run_id or not ID_RE.fullmatch(run_id):
            self._send(HTTPStatus.NOT_FOUND, error_payload("run not found", code="not_found"))
            return
        if operation is None and method == "GET":
            self._send(HTTPStatus.OK, self.service.get_run(run_id))
            return
        if operation == "result" and method == "GET":
            run, lock = self.service.store.get(run_id)
            with lock:
                self._send(HTTPStatus.OK, run.result())
            return
        if operation == "explain" and method == "GET":
            self._send(HTTPStatus.OK, self.service.explain(run_id, parse_qs(parsed.query)))
            return
        if method == "POST" and operation in ("advance", "event", "goal", "fork", "compare", "adopt"):
            payload = self._read_json()
            run, lock = self.service.store.get(run_id)
            with lock:
                revision = payload.pop("expected_revision", None)
                if revision is not None and revision != digest(run.result()):
                    self._send(409, error_payload("Run changed; reopen it before editing", code="revision_conflict"))
                    return
                result = getattr(self.service, operation)(run_id, payload)
            self._send(HTTPStatus.CREATED if operation == "fork" else HTTPStatus.OK, result)
            return
        self._send(HTTPStatus.NOT_FOUND, error_payload("endpoint not found", code="not_found"))

    def _safe_handle(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path
            store = self.service.store
            transaction = store.transaction(method != "GET") if hasattr(store, "transaction") else nullcontext()
            self._buffer_response = True
            try:
                with transaction:
                    self._handle(method)
            finally:
                self._buffer_response = False
            self._send(*self._pending_response)
        except BranchConflict as exc:
            self._send(409, error_payload(str(exc), code="branch_conflict"))
        except KeyError as exc:
            self._send(HTTPStatus.NOT_FOUND, error_payload(str(exc).strip("'"), code="not_found"))
        except (ValueError, TypeError, KeyError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, error_payload(str(exc)))
        except Exception as exc:  # Keep a malformed operation from taking down the server.
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, error_payload("Internal server error", code="server_error"))

    def do_GET(self) -> None:  # noqa: N802
        self._safe_handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._safe_handle("POST")

    def log_message(self, format: str, *args: Any) -> None:
        return


class OperatorServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: OperatorService | None = None) -> None:
        self.service = service or OperatorService()
        super().__init__(address, OperatorHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="PlannerRuntime operator web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default="results/operator.sqlite3")
    args = parser.parse_args()
    from web.persistence import Database, SQLiteRunStore
    database = Database(args.database)
    server = OperatorServer((args.host, args.port), OperatorService(SQLiteRunStore(database)))
    print(f"Planner operator listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
