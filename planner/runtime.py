from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from model.operations import Session, digest, replay_episode

from .baseline import BaselinePlanner
from .strategic import StrategicPlanner, strategic_metadata


RUNTIME_SCHEMA = 'cosmo-B-planner-runtime-1.0'
_GOALS = ('priority', 'revenue')


def _nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be an integer >= 0')
    return value


class PlannerRuntime:
    """A step-boundary controller around a planner and an operations session.

    The runtime deliberately has no event queue.  An event becomes visible only
    after ``apply_event`` succeeds at the session's current step.  This keeps a
    planner's input limited to the current state and the received prefix.
    """

    def __init__(
        self,
        scenario: dict,
        planner: str | Any = 'strategic',
        goal: str = 'priority',
        run_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._validate_goal(goal)
        self.initial_scenario = copy.deepcopy(scenario)
        self.goal = goal
        self._planner_name = self._planner_name_for(planner)
        self.planner = self._build_planner(planner, goal)
        metadata = self._metadata(self._planner_name, goal, run_metadata)
        metadata.setdefault('run_id', digest({'scenario': scenario, 'metadata': metadata})[:16])
        self.session = Session(scenario, metadata)
        self.history: list[dict[str, Any]] = []

    @property
    def env(self) -> Any:
        """Expose the existing environment API without creating a second state."""
        return self.session.env

    @property
    def run_metadata(self) -> dict[str, Any]:
        return self.session.run_metadata

    @property
    def events(self) -> list[dict]:
        return self.session.events

    @property
    def commands(self) -> list[dict]:
        return self.session.commands

    @property
    def current_step(self) -> int:
        return self.session.env.k

    def state_digest(self) -> str:
        return self.session.state_digest()

    @property
    def digest(self) -> str:
        return self.state_digest()

    def observation(self) -> dict:
        observation = self.session.observation()
        observation['planner'] = self._planner_name
        observation['goal'] = self.goal
        return observation

    def apply_event(self, event: dict) -> None:
        """Apply one event at this exact boundary, recording it only on success."""
        step = self.current_step
        self.session.apply_event(event)
        self.history.append({
            'type': 'event',
            'step': step,
            'event': copy.deepcopy(event),
        })

    def step(self) -> list[dict]:
        """Plan and execute exactly one current step."""
        if self.current_step >= self.env.s['time']['steps']:
            raise ValueError('Simulation is finished')
        step = self.current_step
        actions = self.planner.plan(self.session)
        rows = self.session.advance(actions)
        self.history.append({
            'type': 'step',
            'step': step,
            'commands': copy.deepcopy(actions),
            'trace': copy.deepcopy(rows),
        })
        return rows

    execute_step = step

    def run_steps(self, count: int) -> PlannerRuntime:
        count = _nonnegative_integer(count, 'count')
        target = min(self.current_step + count, self.env.s['time']['steps'])
        return self.run_until(target)

    def run_until(self, step: int) -> PlannerRuntime:
        """Execute up to, but not including, ``step``.

        No event is pulled in at the stopping boundary.  The caller can inspect
        the boundary, submit an event, and call this method again.
        """
        step = _nonnegative_integer(step, 'step')
        total = self.env.s['time']['steps']
        if step > total:
            raise ValueError('step cannot exceed scenario length')
        if step < self.current_step:
            raise ValueError('step cannot move the runtime backwards')
        while self.current_step < step:
            self.step()
        return self

    def run(
        self,
        steps: int | None = None,
        until_step: int | None = None,
    ) -> PlannerRuntime:
        """Run a fixed number of steps or to a boundary; default is to finish."""
        if steps is not None and until_step is not None:
            raise ValueError('Provide steps or until_step, not both')
        if until_step is not None:
            return self.run_until(until_step)
        if steps is not None:
            return self.run_steps(steps)
        return self.run_until(self.env.s['time']['steps'])

    def switch_goal(self, goal: str) -> None:
        """Change the strategic objective at the current step boundary."""
        self._validate_goal(goal)
        old_goal = self.goal
        if old_goal == goal:
            raise ValueError(f'goal is already {goal!r}')
        self.goal = goal
        if isinstance(self.planner, StrategicPlanner):
            self.planner = StrategicPlanner(goal)
        self.run_metadata['goal'] = goal
        switches = self.run_metadata.setdefault('goal_switches', [])
        switches.append({'at_step': self.current_step, 'from': old_goal, 'to': goal})
        self.history.append({
            'type': 'goal_switch',
            'step': self.current_step,
            'from': old_goal,
            'to': goal,
        })

    def fork(
        self,
        branch_id: str | None = None,
        planner: str | Any | None = None,
        goal: str | None = None,
    ) -> PlannerRuntime:
        """Copy this actual checkpoint and optionally select a branch policy."""
        if branch_id is not None and (not isinstance(branch_id, str) or not branch_id):
            raise ValueError('branch_id must be a nonempty string')
        if goal is not None:
            self._validate_goal(goal)
        child = copy.deepcopy(self)
        branch_id = branch_id or f'branch-{self.current_step}'
        parent_run_id = self.run_metadata.get('run_id')
        point = {
            'step': self.current_step,
            'state_digest': self.state_digest(),
        }
        child.run_metadata['parent_run_id'] = parent_run_id
        child.run_metadata['parent_branch_point'] = point
        child.run_metadata['parent_branch_point_step'] = point['step']
        child.run_metadata['parent_branch_point_digest'] = point['state_digest']
        child.run_metadata['branch_id'] = branch_id
        child.run_metadata['run_id'] = f'{parent_run_id}/{branch_id}'
        if planner is not None:
            child._planner_name = child._planner_name_for(planner)
            child.planner = child._build_planner(planner, goal or child.goal)
        if goal is not None and goal != child.goal:
            child.goal = goal
            if isinstance(child.planner, StrategicPlanner):
                child.planner = StrategicPlanner(goal)
        child.run_metadata['planner'] = child._planner_name
        child.run_metadata['goal'] = child.goal
        # A branch may change the algorithm; do not export the parent's version
        # and parameters as if they described the continuation.
        child.run_metadata.update(self._metadata(child._planner_name, child.goal, None))
        child.session.run_metadata = child.run_metadata
        return child

    branch = fork

    def replay(self) -> Session:
        """Replay this exported prefix using only its received events/commands."""
        replayed = replay_episode(
            self.initial_scenario,
            self.session.events,
            self.session.commands,
            self.current_step,
        )
        replayed.run_metadata = copy.deepcopy(self.run_metadata)
        return replayed

    def summary(self) -> dict:
        return self.session.summary()

    def result(self) -> dict[str, Any]:
        payload = self.session.result()
        payload['run_metadata'] = copy.deepcopy(self.run_metadata)
        payload['runtime_schema_version'] = RUNTIME_SCHEMA
        payload['history'] = copy.deepcopy(self.history)
        payload['state_digest'] = self.state_digest()
        return payload

    def to_json(self, **kwargs: Any) -> str:
        options = {'ensure_ascii': False, 'indent': 2, 'allow_nan': False}
        options.update(kwargs)
        return json.dumps(self.result(), **options)

    def export(self, path: str | Path) -> dict[str, Any]:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json(), encoding='utf-8')
        return self.result()

    @staticmethod
    def _validate_goal(goal: str) -> None:
        if goal not in _GOALS:
            raise ValueError("goal must be 'priority' or 'revenue'")

    @staticmethod
    def _planner_name_for(planner: str | Any) -> str:
        if isinstance(planner, str):
            if planner not in ('baseline', 'strategic'):
                raise ValueError("planner must be 'baseline' or 'strategic'")
            return planner
        if isinstance(planner, StrategicPlanner):
            return 'strategic'
        if isinstance(planner, BaselinePlanner):
            return 'baseline'
        if not callable(getattr(planner, 'plan', None)):
            raise ValueError('planner must be baseline, strategic, or expose plan(session)')
        return planner.__class__.__name__

    @staticmethod
    def _build_planner(planner: str | Any, goal: str) -> Any:
        if planner == 'baseline':
            return BaselinePlanner()
        if planner == 'strategic':
            return StrategicPlanner(goal)
        if isinstance(planner, (BaselinePlanner, StrategicPlanner)):
            return copy.deepcopy(planner)
        if callable(getattr(planner, 'plan', None)):
            return planner
        raise ValueError('Invalid planner')

    @staticmethod
    def _metadata(
        planner: str,
        goal: str,
        supplied: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if planner == 'strategic':
            metadata = strategic_metadata(goal)
        else:
            metadata = {'planner': 'baseline-v1', 'algorithm': 'baseline', 'goal': goal,
                        'version': 'baseline-v1', 'parameters': {}}
        if supplied:
            extras = copy.deepcopy(supplied)
            for key in ('planner', 'algorithm', 'goal', 'version', 'parameters'):
                extras.pop(key, None)
            metadata.update(extras)
        metadata['planner'] = metadata.get('planner', planner)
        metadata['algorithm'] = metadata.get('algorithm', planner)
        metadata['goal'] = goal
        return metadata


Runtime = PlannerRuntime
OrchestrationRuntime = PlannerRuntime


__all__ = ['PlannerRuntime', 'Runtime', 'OrchestrationRuntime', 'RUNTIME_SCHEMA']
