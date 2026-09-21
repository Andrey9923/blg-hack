from __future__ import annotations

import argparse
import copy
import json
import re
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from model.resource_env import load, validate
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

    def create(self, payload: dict[str, Any]) -> PlannerRuntime:
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        scenario_id = payload.get("scenario_id")
        supplied_scenario = payload.get("scenario")
        if supplied_scenario is not None:
            encoded = json_bytes(supplied_scenario)
            if len(encoded) > MAX_SCENARIO_BYTES:
                raise ValueError("scenario exceeds the 6 MiB size limit")
            if not isinstance(supplied_scenario, dict):
                raise ValueError("scenario must be a JSON object")
            scenario = copy.deepcopy(supplied_scenario)
            scenario_id = scenario.get("meta", {}).get("id", scenario_id)
            if not isinstance(scenario_id, str) or not scenario_id:
                raise ValueError("scenario.meta.id or scenario_id is required")
        elif isinstance(scenario_id, str) and scenario_id in self._scenarios:
            scenario = copy.deepcopy(self._scenarios[scenario_id])
        else:
            raise ValueError("scenario_id must be one of P01_intro, P02_shift, P03_energy, P04_demand")
        if not isinstance(scenario_id, str) or not ID_RE.fullmatch(scenario_id):
            raise ValueError("scenario id contains unsupported characters")
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
        with self._lock:
            if len(self._runs) >= MAX_RUNS:
                raise ValueError("in-memory run limit reached; restart the server to clear runs")
            run_id = branch.run_metadata.get("run_id")
            if not isinstance(run_id, str) or run_id in self._runs:
                raise ValueError("fork generated a duplicate run id")
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
    return {
        "run_id": run.run_metadata.get("run_id"),
        "observation": run.observation(),
        "summary": run.summary(),
        "metadata": copy.deepcopy(run.run_metadata),
        "history": copy.deepcopy(run.history),
        "available_actions": available_actions(run),
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
            self.store.add_fork(run_id, child)
            return run_view(child)

    def explain(self, run_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        run, lock = self.store.get(run_id)
        try:
            step = int(query.get("step", [""])[0])
        except (TypeError, ValueError):
            raise ValueError("step query parameter must be an integer")
        satellite_id = query.get("satellite_id", [""])[0]
        with lock:
            return explain_trace(run, step, satellite_id)


class OperatorHandler(BaseHTTPRequestHandler):
    server_version = "PlannerOperator/1.0"

    @property
    def service(self) -> OperatorService:
        return self.server.service  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
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
        if method == "POST" and operation in ("advance", "event", "goal", "fork"):
            payload = self._read_json()
            result = getattr(self.service, operation)(run_id, payload)
            self._send(HTTPStatus.CREATED if operation == "fork" else HTTPStatus.OK, result)
            return
        self._send(HTTPStatus.NOT_FOUND, error_payload("endpoint not found", code="not_found"))

    def _safe_handle(self, method: str) -> None:
        try:
            self._handle(method)
        except KeyError as exc:
            self._send(HTTPStatus.NOT_FOUND, error_payload(str(exc).strip("'"), code="not_found"))
        except (ValueError, TypeError, KeyError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, error_payload(str(exc)))
        except Exception as exc:  # Keep a malformed operation from taking down the server.
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, error_payload(f"server error: {exc}", code="server_error"))

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
    args = parser.parse_args()
    server = OperatorServer((args.host, args.port))
    print(f"Planner operator listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
