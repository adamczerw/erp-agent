"""Tests for DolibarrAdapter error handling, run through the real openadapt-evals runner."""

from __future__ import annotations

import numpy as np
import pytest
from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkResult, BenchmarkTask
from openadapt_evals.agents.base import BenchmarkAgent
from openadapt_evals.benchmarks import runner

from erp_agent.adapter.adapter import DolibarrAdapter, InfrastructureError, InvalidActionError
from erp_agent.desktop.vnc_connection import RfbError, keysym
from erp_agent.desktop.vnc_desktop import VncDesktop

DONE = BenchmarkAction("done")


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setattr("openadapt_evals.telemetry.capture_usage_event", lambda **kwargs: False)


class FakeConn:
    width, height = 100, 100

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent = []

    def _send(self, *event):
        if self.fail:
            raise RfbError("connection lost")
        self.sent.append(event)

    def pointer(self, x, y, buttons=0):
        self._send("pointer", x, y, buttons)

    def key(self, sym, down):
        self._send("key", sym, down)

    def sync(self):
        self._send("sync")

    def wait_for_update(self, timeout):
        if self.fail:
            raise RfbError("connection lost")
        return None

    def frame(self):
        return np.zeros((self.height, self.width, 3), np.uint8)


class FakeAdapter(DolibarrAdapter):
    def __init__(self, conn: FakeConn):
        self.vnc_desktop = VncDesktop.__new__(VncDesktop)
        self.vnc_desktop.conn = conn
        self._step_count = 0
        self._wait_seconds = 0
        self.evaluated = 0

    def reset(self, task):
        return self.observe()

    def evaluate(self, task):
        self.evaluated += 1
        return BenchmarkResult(task_id=task.task_id, success=True, score=1.0)


class ScriptedAgent(BenchmarkAgent):
    def __init__(self, actions: list[BenchmarkAction]):
        self.actions = list(actions)
        self.seen = []

    def act(self, observation, task, history=None):
        self.seen.append(observation.raw_observation)
        return self.actions.pop(0)

    def reset(self):
        pass


def _run(actions, conn=None):
    adapter = FakeAdapter(conn or FakeConn())
    agent = ScriptedAgent(actions)
    task = BenchmarkTask(task_id="t", instruction="i", domain="d")
    result = runner._run_single_task(agent, adapter, task, runner.EvaluationConfig(max_steps=20))
    return result, adapter, agent


def test_connection_failure_raises_infrastructure_error():
    adapter = FakeAdapter(FakeConn(fail=True))
    with pytest.raises(InfrastructureError, match="connection lost") as excinfo:
        adapter.step(BenchmarkAction("click", x=1, y=1))
    assert isinstance(excinfo.value.__cause__, RfbError)


def test_connection_failure_stops_task_without_scoring():
    result, adapter, _ = _run([BenchmarkAction("click", x=1, y=1), DONE], FakeConn(fail=True))
    assert result.error_type == "infrastructure"
    assert not result.success
    assert adapter.evaluated == 0


@pytest.mark.parametrize(
    "action, message",
    [
        (BenchmarkAction("click", y=1), "missing x"),
        (BenchmarkAction("double_click", x=1), "missing y"),
        (BenchmarkAction("type"), "missing text"),
        (BenchmarkAction("key"), "missing key"),
        (BenchmarkAction("scroll"), "missing scroll_direction"),
        (BenchmarkAction("scroll", scroll_direction="sideways"), "unknown scroll direction"),
        (BenchmarkAction("drag", x=1, y=1, end_x=2), "missing end_y"),
        (BenchmarkAction("foo"), "unknown action type"),
        (BenchmarkAction("click", x=0.5, y=0.5), "x=0.5 is not a pixel"),
        (BenchmarkAction("click", x=1, y=100), "y=100 is not a pixel"),
        (BenchmarkAction("drag", x=1, y=1, end_x=-1, end_y=2), "end_x=-1 is not a pixel"),
    ],
)
def test_malformed_action_stops_task(action, message):
    conn = FakeConn()
    result, adapter, _ = _run([action, DONE], conn)
    assert result.error_type == "agent"
    assert message in result.error
    assert adapter.evaluated == 0
    assert conn.sent == []


def test_malformed_action_raises_invalid_action_error():
    with pytest.raises(InvalidActionError):
        FakeAdapter(FakeConn()).step(BenchmarkAction("foo"))


@pytest.mark.parametrize(
    "action, message",
    [
        (BenchmarkAction("type", text="abc\r"), "no keysym"),
        (BenchmarkAction("key", key="notakey"), "unknown key name"),
        (BenchmarkAction("key", key="a", modifiers=["notamod"]), "unknown key name"),
    ],
)
def test_untypeable_input_gives_feedback_for_one_step(action, message):
    conn = FakeConn()
    result, adapter, agent = _run([action, BenchmarkAction("wait"), DONE], conn)
    assert message in agent.seen[1]["action_error"]
    assert agent.seen[2] is None
    assert conn.sent == []
    assert result.error is None
    assert adapter.evaluated == 1


def test_valid_actions_run_without_errors():
    conn = FakeConn()
    result, adapter, agent = _run(
        [
            BenchmarkAction("click", x=1, y=1),
            BenchmarkAction("double_click", x=1, y=1),
            BenchmarkAction("right_click", x=1, y=1),
            BenchmarkAction("type", text="ab\n"),
            BenchmarkAction("key", key="a", modifiers=["ctrl"]),
            BenchmarkAction("scroll", scroll_direction="down"),
            BenchmarkAction("drag", x=1, y=1, end_x=2, end_y=2),
            BenchmarkAction("wait"),
            DONE,
        ],
        conn,
    )
    assert result.success
    assert result.error is None
    assert result.error_type is None
    assert adapter.evaluated == 1
    assert all(seen is None for seen in agent.seen)
    assert conn.sent


@pytest.mark.parametrize(
    "key, modifiers, sent",
    [
        ("Tab", ["shift"], "iso_left_tab"),
        ("tab", ["ctrl", "shift"], "iso_left_tab"),
        ("a", ["shift"], "A"),
        ("a", ["ctrl", "Shift"], "A"),
        ("A", ["ctrl", "shift"], "A"),
        ("End", ["shift"], "End"),
        ("Tab", None, "Tab"),
        ("a", ["ctrl"], "a"),
    ],
)
def test_press_sends_the_key_shift_needs(key, modifiers, sent):
    conn = FakeConn()
    FakeAdapter(conn).vnc_desktop.press(key, modifiers)
    held = [keysym(modifier) for modifier in modifiers or []]
    downs = [("key", sym, True) for sym in held]
    ups = [("key", sym, False) for sym in reversed(held)]
    assert conn.sent == [
        *downs,
        ("key", keysym(sent), True),
        ("key", keysym(sent), False),
        *ups,
        ("sync",),
    ]


def test_empty_key_is_a_no_op():
    conn = FakeConn()
    obs, done, _ = FakeAdapter(conn).step(BenchmarkAction("key", key=""))
    assert conn.sent == []
    assert not done
    assert obs.raw_observation is None


def test_tasks_load_from_json(tmp_path):
    (tmp_path / "smoke").mkdir()
    (tmp_path / "smoke" / "a.json").write_text('{"task_id": "a", "instruction": "i", "domain": "desktop"}')
    (tmp_path / "b.json").write_text('{"task_id": "b", "instruction": "i", "domain": "web"}')
    adapter = DolibarrAdapter(VncDesktop.__new__(VncDesktop), tasks_dir=tmp_path)
    assert [task.task_id for task in adapter.list_tasks()] == ["b", "a"]
    assert [task.task_id for task in adapter.list_tasks("desktop")] == ["a"]
    assert adapter.load_task("a").domain == "desktop"
    with pytest.raises(KeyError):
        adapter.load_task("missing")
