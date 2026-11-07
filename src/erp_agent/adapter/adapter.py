from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
import os

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)

from erp_agent.desktop.vnc_connection import RfbError
from erp_agent.desktop.vnc_desktop import SCROLL_MAP, VncDesktop
from erp_agent.dolibarr.dolibarr import Dolibarr

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {
    "click": ("x", "y"),
    "double_click": ("x", "y"),
    "right_click": ("x", "y"),
    "type": ("text",),
    "key": ("key",),
    "scroll": ("scroll_direction",),
    "drag": ("x", "y", "end_x", "end_y"),
    "done": (),
    "wait": (),
    "error": (),
}


class InfrastructureError(Exception):
    error_type = "infrastructure"


class InvalidActionError(Exception):
    error_type = "agent"


class DolibarrAdapter(BenchmarkAdapter):

    def __init__(
        self,
        desktop: VncDesktop,
        backend: Dolibarr | None = None,
        tasks_dir: str | Path = "tasks",
        wait_seconds: float = 3.0,
    ):
        self.vnc_desktop = desktop
        self.backend = backend
        self.tasks_dir = Path(tasks_dir)
        self._step_count = 0
        self._wait_seconds = wait_seconds


    @property
    def name(self) -> str:
        return "dolibarr"

    @property
    def benchmark_type(self) -> str:
        return "interactive"


    def observe(self) -> BenchmarkObservation:
        return BenchmarkObservation(
            screenshot=self.vnc_desktop.screenshot(),
            viewport=[self.vnc_desktop.width, self.vnc_desktop.height]
        )

    
    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        """Execute an action on the Dolibarr environment and return a new observation.

        Supported action types:
        - ``click``: Move mouse to (x, y) and click.
        - ``double_click``: Move mouse to (x, y) and double-click.
        - ``right_click``: Move mouse to (x, y) and right-click.
        - ``type``: Type text string.
        - ``key``: Press a single key, optionally with modifiers.
        - ``scroll``: Scroll in a direction.
        - ``drag``: Drag from (x, y) to (end_x, end_y).
        - ``done``: Signal task completion (no-op).

        Args:
            action: The action to execute.

        Returns:
            Tuple of (observation, done, info). A key or text that can't be typed is
            reported in ``observation.raw_observation["action_error"]``.

        Raises:
            InvalidActionError: The action is malformed.
            InfrastructureError: The VNC connection failed.
        """

        self._step_count += 1
        done = action.type in ("done", "error")
        action_error = None

        try:
            try:
                self._execute_action(action)
            except ValueError as e:
                logger.warning("Action %s rejected: %s", action.type, e)
                action_error = str(e)
            if not done:
                self.vnc_desktop.wait_until_stable()
        except RfbError as e:
            raise InfrastructureError(str(e)) from e

        obs = self.observe()
        if action_error:
            obs.raw_observation = {"action_error": action_error}
        return obs, done, {"step": self._step_count}


    def _validate_action(self, action: BenchmarkAction) -> None:
        """Raise InvalidActionError if the action type is unknown or fields are missing."""
        if action.type not in REQUIRED_FIELDS:
            raise InvalidActionError(f"unknown action type: {action.type}")

        missing = [field for field in REQUIRED_FIELDS[action.type] if getattr(action, field) is None]
        if missing:
            raise InvalidActionError(f"{action.type} is missing {', '.join(missing)}")

        if action.type == "scroll" and action.scroll_direction not in SCROLL_MAP:
            raise InvalidActionError(f"unknown scroll direction: {action.scroll_direction}")

        width, height = self.vnc_desktop.width, self.vnc_desktop.height
        for field, limit in (("x", width), ("y", height), ("end_x", width), ("end_y", height)):
            value = getattr(action, field)
            if value is not None and (value != int(value) or not 0 <= value < limit):
                raise InvalidActionError(f"{field}={value} is not a pixel on the {width}x{height} screen")


    def _execute_action(self, action: BenchmarkAction):
        """Dispatch and execute a single action via VncDesktop."""
        self._validate_action(action)
        action_type = action.type

        if action_type in ("click", "double_click", "right_click"):
            self._do_click(action)
        elif action_type == "type":
            self._do_type(action)
        elif action_type == "key":
            self._do_key(action)
        elif action_type == "scroll":
            self._do_scroll(action)
        elif action_type == "drag":
            self._do_drag(action)
        elif action_type == "wait":
            time.sleep(self._wait_seconds)

    def _do_click(self, action: BenchmarkAction) -> None:
        """Execute a mouse click action."""

        x = action.x
        y = action.y

        if action.type == "double_click":
            self.vnc_desktop.double_click(x, y)
        elif action.type == "right_click":
            self.vnc_desktop.click(x, y, "right")
        else:
            self.vnc_desktop.click(x, y, "left")


    def _do_type(self, action: BenchmarkAction) -> None:
        """Execute a text typing action."""
        self.vnc_desktop.type(action.text)


    def _do_key(self, action: BenchmarkAction) -> None:
        """Execute a key press, optionally with modifiers."""
        if action.key:
            self.vnc_desktop.press(action.key, action.modifiers)


    def _do_scroll(self, action: BenchmarkAction) -> None:
        """Execute a scroll, at the screen center if the action has no position."""
        x = action.x if action.x is not None else self.vnc_desktop.width // 2
        y = action.y if action.y is not None else self.vnc_desktop.height // 2

        if action.scroll_amount is None:
            self.vnc_desktop.scroll(x, y, action.scroll_direction)
        else:
            self.vnc_desktop.scroll(x, y, action.scroll_direction, int(action.scroll_amount))


    def _do_drag(self, action: BenchmarkAction) -> None:
        """Execute a drag from (x, y) to (end_x, end_y)."""
        self.vnc_desktop.drag(action.x, action.y, action.end_x, action.end_y)


    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        """Run benchmark's native evaluation on current state.

        Args:
            task: Task to evaluate.

        Returns:
            BenchmarkResult with success/score.
        """
        return BenchmarkResult(task_id=task.task_id, success=False, score=0.0, reason="not evaluated")

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        """List available tasks, optionally filtered by domain.

        Args:
            domain: Optional domain filter (e.g., "browser", "office").

        Returns:
            List of BenchmarkTask objects.
        """
        tasks = [BenchmarkTask(**json.loads(path.read_text())) for path in sorted(self.tasks_dir.rglob("*.json"))]
        return [task for task in tasks if domain is None or task.domain == domain]

    
    def load_task(self, task_id: str) -> BenchmarkTask:
        """Load a specific task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            BenchmarkTask object.

        Raises:
            KeyError: If task_id not found.
        """
        return {task.task_id: task for task in self.list_tasks()}[task_id]

    
    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        """Reset environment to task's initial state.

        Args:
            task: Task to initialize.

        Returns:
            Initial observation.
        """
        try:
            self.vnc_desktop.reconnect()
        except RfbError as e:
            raise InfrastructureError(str(e)) from e
        self._step_count = 0
        return self.observe()





if __name__ == "__main__":
    load_dotenv()
    VNC_PASSWORD = os.environ["VNC_PASSWORD"]
    VNC_ADDRESS = "127.0.0.1:57257/proxy/6080"
    desktop = VncDesktop(VNC_ADDRESS, VNC_PASSWORD)
    adapter = DolibarrAdapter(desktop)
    # ss = adapter.observe().screenshot
    # with open("screenshot.png", "wb") as f:
    #     f.write(ss)
    demo = [
        ("click: select the File System icon", BenchmarkAction("click", x=70, y=75), 6),
        ("drag: rubber-band select both icons", BenchmarkAction("drag", x=20, y=40, end_x=130, end_y=240), 3),
        ("click: clear the selection", BenchmarkAction("click", x=300, y=300), 3),
        ("right_click: open the desktop menu", BenchmarkAction("right_click", x=300, y=300), 6),
        ("key: Escape closes the menu", BenchmarkAction("key", key="Escape"), 3),
        ("double_click: open Home in the file manager", BenchmarkAction("double_click", x=70, y=180), 4),
        ("key with modifier: ctrl+w closes the file manager", BenchmarkAction("key", key="w", modifiers=["ctrl"]), 3),
        ("click: open a terminal from the dock", BenchmarkAction("click", x=568, y=776), 4),
        ("type: a command, no Enter yet", BenchmarkAction("type", text="seq 1 200"), 3),
        ("key: Enter runs it", BenchmarkAction("key", key="Enter"), 3),
        ("scroll: 5 clicks up at the screen center", BenchmarkAction("scroll", scroll_direction="up", scroll_amount=5), 3),
        ("scroll: 5 clicks down at the screen center", BenchmarkAction("scroll", scroll_direction="down", scroll_amount=5), 3),
        ("key with modifier: ctrl+l clears the terminal", BenchmarkAction("key", key="l", modifiers=["ctrl"]), 3),
        ("type: \\n presses Enter, exit closes the terminal", BenchmarkAction("type", text="exit\n"), 3),
        ("done: no-op", BenchmarkAction("done"), 0),
    ]
    for description, action, pause in demo:
        print(description)
        adapter._execute_action(action)
        time.sleep(pause)

    desktop.close()