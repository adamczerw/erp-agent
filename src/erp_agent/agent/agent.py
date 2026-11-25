"""ErpAgent: the BenchmarkAgent the runner talks to.

Turns ``PlannerDecision`` into ``BenchmarkAction`` (with the grounder for clicks) and tells
the planner what actually ran. Variants are configurations, not subclasses:

    ErpAgent(Planner(..., grounded=False))                       # planner only
    ErpAgent(Planner(..., grounded=True), grounder)              # planner + grounder
    ErpAgent(Planner(..., grounded=False, demo=demo))            # planner + demo
    ErpAgent(Planner(..., grounded=True, demo=demo), grounder)   # planner + grounder + demo
"""

from __future__ import annotations

import logging

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation, BenchmarkTask
from openadapt_evals.agents.base import BenchmarkAgent

from erp_agent.agent.planner import Planner, PlannerDecision

logger = logging.getLogger(__name__)


def to_pixels(x: float, y: float, viewport: tuple[int, int]) -> tuple[int, int]:
    """Normalized 0-1 coordinates -> pixel on the screen, clamped to it.

    Grounders return 0-1, the adapter takes pixels (decision of 7.11). Also needed by the
    wrapper that runs the upstream baseline.
    """
    raise NotImplementedError


class ErpAgent(BenchmarkAgent):
    """Planner -> (grounder for clicks) -> BenchmarkAction.

    Args:
        planner: Decides the next action and keeps the context.
        grounder: Resolves ``decision.target`` to 0-1 coordinates (``OmniParserGrounder``
            or any ``BenchmarkAgent`` that returns a click). Must be given exactly when
            ``planner.grounded`` is True.
    """

    def __init__(self, planner: Planner, grounder: BenchmarkAgent | None = None):
        if planner.grounded != (grounder is not None):
            raise ValueError("a grounder is needed exactly when planner.grounded is True")
        self.planner = planner
        self.grounder = grounder

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Return the next action. ``history`` from the runner is ignored: the planner keeps its own.

        1. ``observation.raw_observation["action_error"]`` (key or text that couldn't be
           typed in the previous step) -> ``planner.add_feedback``.
        2. ``decision = planner.plan(observation, task)``.
        3. ``action, executed, feedback = self._to_action(decision, observation)``.
        4. ``planner.record(decision, executed, feedback)``.
        5. ``action.raw_action`` = planner output, reasoning and grounding details, for traces.
        """
        raise NotImplementedError

    def reset(self) -> None:
        """Called by the runner before every task."""
        self.planner.reset()

    def _to_action(
        self, decision: PlannerDecision, observation: BenchmarkObservation
    ) -> tuple[BenchmarkAction, str, str | None]:
        """Map a decision to (action, what ran as text for the history, feedback or None).

        DONE                 -> BenchmarkAction("done")
        FAIL                 -> BenchmarkAction("error", raw_action={"error_type": "agent",
                                "reason": decision.reasoning})   # runner.py:513 -> not a success
        wait                 -> BenchmarkAction("wait")  (also an invalid response:
                                ``decision.invalid`` is then the feedback)
        click / double_click -> grounded: self._ground(...); else decision.x, decision.y
        type / key / scroll  -> straight from the fields
        """
        raise NotImplementedError

    def _ground(
        self, decision: PlannerDecision, observation: BenchmarkObservation
    ) -> tuple[BenchmarkAction, str, str | None]:
        """Resolve ``decision.target`` with the grounder.

        - ``self.grounder.act(observation, BenchmarkTask("grounder", decision.target, "web"))``
        - returned ``wait`` = grounding failed -> keep ``wait``, feedback
          ``"grounding failed: <raw_action['grounding_failed']>"``
        - click -> ``to_pixels`` with ``observation.viewport``; keep ``decision.action_type``
          (double_click) and the grounder's ``raw_action`` (element index, text, bounds)
        """
        raise NotImplementedError
