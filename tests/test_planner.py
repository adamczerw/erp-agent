"""Tests for Planner prompt building, response parsing and plan() (no API calls)."""

import json
import re

import pytest
from openadapt_evals.adapters.base import BenchmarkObservation, BenchmarkTask

from erp_agent.agent import planner as planner_module
from erp_agent.agent.planner import ENVIRONMENT_DOLIBARR, Planner

TASK = BenchmarkTask(task_id="t", instruction='Create a third party named "Acme".', domain="web")
SCREEN = (1280, 800)


def _response(**fields):
    base = {"observation": "a form", "previous_result": "first step", "reasoning": "next"}
    return json.dumps({**base, **fields})


def _parse(grounded=True, **fields):
    return Planner("m", grounded=grounded)._parse(_response(**fields), SCREEN)


@pytest.mark.parametrize("fields, expected", [
    ({"action_type": "click", "target": "Save button"}, {"target": "Save button"}),
    ({"action_type": "double_click", "target": "Acme"}, {"target": "Acme"}),
    ({"action_type": "type", "text": "Acme\n"}, {"text": "Acme\n"}),
    ({"action_type": "key", "key": "a", "modifiers": ["Ctrl"]}, {"key": "a", "modifiers": ["ctrl"]}),
    ({"action_type": "key", "key": "Enter"}, {"key": "Enter", "modifiers": None}),
    ({"action_type": "scroll", "scroll_direction": "down"}, {"scroll_direction": "down"}),
    ({"action_type": "wait"}, {}),
])
def test_parse_valid_commands(fields, expected):
    decision = _parse(decision="COMMAND", **fields)
    assert decision.invalid is None
    assert decision.action_type == fields["action_type"]
    for name, value in expected.items():
        assert getattr(decision, name) == value
    assert decision.observation == "a form" and decision.reasoning == "next"


def test_parse_drops_fields_of_other_actions():
    decision = _parse(decision="COMMAND", action_type="click", target="Save", text="junk", x=5, y=5)
    assert decision.target == "Save"
    assert decision.text is None and decision.x is None


def test_parse_ungrounded_click_takes_pixels():
    decision = _parse(grounded=False, decision="COMMAND", action_type="click", x=640.0, y=400)
    assert (decision.x, decision.y) == (640, 400)
    assert decision.target is None


@pytest.mark.parametrize("decision", ["DONE", "FAIL"])
def test_parse_done_and_fail_need_no_action(decision):
    parsed = _parse(decision=decision, action_type="click")
    assert parsed.decision == decision
    assert parsed.action_type is None and parsed.invalid is None


@pytest.mark.parametrize("decision", ["done", "Done", " DONE ", "dOnE"])
def test_parse_decision_ignores_case_and_spaces(decision):
    assert _parse(decision=decision).decision == "DONE"


def test_parse_action_type_and_direction_ignore_case():
    decision = _parse(decision=" command", action_type="Scroll ", scroll_direction="DOWN")
    assert (decision.decision, decision.action_type, decision.scroll_direction) == ("COMMAND", "scroll", "down")


def test_parse_reasoning_fields_are_optional():
    raw = json.dumps({"decision": "COMMAND", "action_type": "click", "target": "Save", "reasoning": None})
    decision = Planner("m")._parse(raw, SCREEN)
    assert decision.invalid is None and decision.target == "Save"
    assert decision.observation == decision.previous_result == decision.reasoning == ""


def test_parse_accepts_json_in_a_code_fence():
    raw = "Here is my answer:\n```json\n" + _response(decision="DONE") + "\n```"
    assert Planner("m")._parse(raw, SCREEN).decision == "DONE"


@pytest.mark.parametrize("raw, reason", [
    ("not json at all", "JSON object"),
    ('["a list"]', "JSON object"),
    (json.dumps({"observation": "o", "reasoning": "r"}), "'decision'"),
    (_response(decision="finished"), "'decision'"),
    (_response(decision=1), "'decision'"),
    (_response(decision="COMMAND"), "action_type"),
    (_response(decision="COMMAND", action_type=None), "unknown action_type"),
    (_response(decision="COMMAND", action_type="drag"), "unknown action_type"),
    (_response(decision="COMMAND", action_type="click"), "'target'"),
    (_response(decision="COMMAND", action_type="click", target="  "), "'target'"),
    (_response(decision="COMMAND", action_type="type", text=""), "'text'"),
    (_response(decision="COMMAND", action_type="key"), "'key'"),
    (_response(decision="COMMAND", action_type="key", key="a", modifiers="ctrl"), "list"),
    (_response(decision="COMMAND", action_type="key", key="4", modifiers=["super"]), "unknown modifiers"),
    (_response(decision="COMMAND", action_type="scroll", scroll_direction="left"), "scroll_direction"),
])
def test_parse_rejects_invalid_response(raw, reason):
    with pytest.raises(ValueError, match=re.escape(reason)):
        Planner("m")._parse(raw, SCREEN)


@pytest.mark.parametrize("x, y", [(1280, 10), (10, 800), (-1, 10), (10.5, 10), ("10", 10), (True, 10), (None, 10)])
def test_parse_ungrounded_click_rejects_bad_pixels(x, y):
    with pytest.raises(ValueError):
        _parse(grounded=False, decision="COMMAND", action_type="click", x=x, y=y)


class FakeVlm:
    """Stands in for vlm_call: records the call and returns a fixed response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


OBSERVATION = BenchmarkObservation(screenshot=b"png", viewport=SCREEN)


def test_plan_calls_the_vlm_with_prompt_and_screenshot(monkeypatch):
    vlm = FakeVlm(_response(decision="COMMAND", action_type="click", target="Save"))
    monkeypatch.setattr(planner_module, "vlm_call", vlm)
    planner = Planner("model-x", provider="anthropic", max_tokens=777)

    decision = planner.plan(OBSERVATION, TASK)

    assert decision.target == "Save" and decision.invalid is None
    (call,) = vlm.calls
    system, prompt = planner._build_prompt(TASK, SCREEN)
    assert (call["prompt"], call["system"]) == (prompt, system)
    assert call["images"] == [b"png"]
    assert (call["model"], call["provider"], call["max_tokens"]) == ("model-x", "anthropic", 777)
    assert call["cost_label"] == "planner"


def test_plan_turns_an_unusable_response_into_wait_once(monkeypatch):
    vlm = FakeVlm(_response(decision="COMMAND", action_type="click"))
    monkeypatch.setattr(planner_module, "vlm_call", vlm)

    decision = Planner("m").plan(OBSERVATION, TASK)

    assert len(vlm.calls) == 1  # no retry
    assert (decision.decision, decision.action_type) == ("COMMAND", "wait")
    assert decision.invalid == "invalid output: click needs a non-empty 'target'"
    assert decision.raw == vlm.response


def test_grounded_prompt_asks_for_target_not_coordinates():
    system, _ = Planner("m", grounded=True)._build_prompt(TASK, (1280, 800))
    assert '"target"' in system
    assert '"x", "y"' not in system


def test_coordinates_prompt_uses_the_viewport_size():
    system, _ = Planner("m", grounded=False)._build_prompt(TASK, (1024, 768))
    assert '"x", "y"' in system
    assert "1024x768" in system
    assert "{width}" not in system and "{{" not in system


def test_environment_section_is_optional():
    general, _ = Planner("m")._build_prompt(TASK, (1280, 800))
    dolibarr, _ = Planner("m", environment=ENVIRONMENT_DOLIBARR)._build_prompt(TASK, (1280, 800))
    assert "Dolibarr" not in general
    assert ENVIRONMENT_DOLIBARR in dolibarr


def test_user_prompt_has_task_first_step_and_format_at_the_end():
    _, prompt = Planner("m")._build_prompt(TASK, (1280, 800))
    assert f"<task>\n{TASK.instruction}\n</task>" in prompt
    assert "first step" in prompt
    assert "<demo>" not in prompt
    assert prompt.rstrip().endswith('leave out "action_type" and the action fields.')


def test_demo_goes_before_the_task_and_keeps_its_braces():
    demo = '1. click "New" {in the left menu}'
    _, prompt = Planner("m", demo=demo)._build_prompt(TASK, (1280, 800))
    assert demo in prompt
    assert prompt.index("<demo>") < prompt.index("<task>")


def _command(action_type, **fields):
    return planner_module.PlannerDecision("COMMAND", "r", action_type=action_type, **fields)


def test_history_lines_describe_decision_execution_and_feedback():
    planner = Planner("m")
    planner.record(_command("click", target="New Third Party"), "click (70, 233)")
    planner.record(_command("type", text='A "B"\n'), 'type "A \\"B\\"\\n"')
    planner.record(_command("key", key="a", modifiers=["ctrl"]), "key ctrl+a")
    planner.record(_command("scroll", scroll_direction="down"), "scroll down")
    planner.record(_command("click", target="Save"), "wait", "grounding failed: no matching element")
    planner.record(planner_module.PlannerDecision("DONE", "r"), "done")

    assert planner._format_history().splitlines() == [
        '1. click "New Third Party" -> click (70, 233)',
        '2. type "A \\"B\\"\\n"',
        "3. key ctrl+a",
        "4. scroll down",
        '5. click "Save" -> wait (grounding failed: no matching element)',
        "6. DONE -> done",
    ]


def test_history_shows_only_the_last_steps_with_their_numbers():
    planner = Planner("m", max_history=2)
    for _ in range(5):
        planner.record(_command("wait"), "wait")
    assert planner._format_history().splitlines() == ["(3 earlier steps not shown)", "4. wait", "5. wait"]


def test_invalid_output_shows_its_reason():
    planner = Planner("m")
    invalid = Planner._invalid("raw", "click needs a non-empty 'target'")
    planner.record(invalid, "wait", invalid.invalid)
    assert planner._format_history() == "1. wait (invalid output: click needs a non-empty 'target')"


def test_add_feedback_goes_to_the_last_step():
    planner = Planner("m")
    planner.add_feedback("ignored: no step yet")
    planner.record(_command("key", key="F13"), "key F13")
    planner.add_feedback("unknown key name: 'F13'")
    planner.add_feedback("second note")
    assert planner.history[-1].feedback == "unknown key name: 'F13'; second note"


def test_user_prompt_contains_the_history():
    planner = Planner("m")
    planner.record(_command("click", target="Save"), "click (1, 2)")
    _, prompt = planner._build_prompt(TASK, SCREEN)
    assert '<history>\n1. click "Save" -> click (1, 2)\n</history>' in prompt
    planner.reset()
    assert "first step" in planner._build_prompt(TASK, SCREEN)[1]
