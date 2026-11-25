"""Planner: builds the prompt, calls the VLM, parses its decision and keeps the step history.

Parts of the prompt are adapted from openadapt-evals
``openadapt_evals/agents/planner_grounder_agent.py`` (MIT License, (c) 2024-2025 OpenAdaptAI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, get_args

from openadapt_evals.adapters.base import BenchmarkObservation, BenchmarkTask
from openadapt_evals.vlm import extract_json, vlm_call

Decision = Literal["COMMAND", "DONE", "FAIL"]
ActionType = Literal["click", "double_click", "type", "key", "scroll", "wait"]
MODIFIERS = ("ctrl", "alt", "shift")
SCROLL_DIRECTIONS = ("up", "down")


@dataclass
class PlannerDecision:
    """One parsed and validated planner response."""

    decision: Decision
    reasoning: str
    observation: str = ""
    previous_result: str = ""
    action_type: ActionType | None = None  # None for DONE / FAIL
    target: str | None = None              # grounded click
    x: int | None = None                   # ungrounded click, pixels
    y: int | None = None
    text: str | None = None                # "\n" presses Enter
    key: str | None = None
    modifiers: list[str] | None = None
    scroll_direction: str | None = None
    raw: str = ""                          # raw model output
    invalid: str | None = None             # why the response was unusable; the decision is then a wait


@dataclass
class StepRecord:
    """One history entry: what the planner decided and what actually happened."""

    decision: PlannerDecision
    executed: str                # e.g. "click (70, 233)"
    feedback: str | None = None  # e.g. "grounding failed: ...", "invalid output: ..."


# The system prompt sections go through str.format(width=..., height=...): double literal braces.

SYSTEM_PROMPT = """\
You are an ERP automation planner. You operate an ERP application through its graphical \
user interface, one action at a time. At each step you get the task, the history of your \
previous steps and a screenshot of the current screen ({width}x{height} pixels). Decide the \
single next action that advances the task. A separate component executes it, and you see \
the result in the next screenshot.

Follow the task instruction exactly and be precise and unambiguous. Only interact with the \
parts of the interface that the task needs. You only see the screenshot: there is no HTML \
or accessibility tree. The mouse pointer may be visible in it.
"""

ENVIRONMENT_DOLIBARR = """\
Environment:
- The application is Dolibarr ERP. It runs in a Chrome window in app mode: there is no \
address bar, no tabs and no browser toolbar. Navigate only with Dolibarr's own menus, links \
and buttons.
- The interface is in English. You are already logged in as the administrator.
- The top menu selects a module (e.g. Third parties, Products). The left menu shows the \
actions of the selected module, such as "New ..." and "List".
- Forms can be longer than the screen: the save button (e.g. "Create ...", "Save") is \
usually at the bottom, so you may need to scroll down. Labels of required fields are bold.
- After a successful save Dolibarr shows the record's card. Errors appear as a message \
near the top of the page.
"""

_ACTIONS_COMMON = """\
- type: type "text" into the focused field. Click the field first unless it is clearly \
focused (blinking cursor). Typing doesn't clear the field: to replace its content, select \
it first with key Ctrl+A. A "\\n" in "text" presses Enter. Use plain ASCII characters only. \
All text goes through type, including capital letters and symbols: write them as they are \
(e.g. "Acme", "A&B Ltd."), never as key with shift.
- key: press "key", optionally with "modifiers", e.g. {{"key": "a", "modifiers": ["ctrl"]}}. \
Use it for special keys and shortcuts, not for entering text. Keys: Enter, Tab, Escape, \
Backspace, Delete, Home, End, PageUp, PageDown, Up, Down, Left, Right, Space, or a single \
character. Modifiers: ctrl, alt, shift.
- scroll: scroll the page in "scroll_direction" ("up" or "down"), about half a screen.
- wait: do nothing for about 3 seconds and take a new screenshot. No other fields.
"""

ACTION_SPACE_GROUNDED = """\
Actions ("action_type" and its fields):
- click: click an element. "target" describes the exact element: its button label, menu \
item name, link text, field label or placeholder, or a precise visual description if it \
has no text. If the same text appears more than once, say where (e.g. "New in the left \
menu"). Never give coordinates: another model finds the element from "target".
- double_click: like click, with a double click.
""" + _ACTIONS_COMMON

ACTION_SPACE_COORDINATES = """\
Actions ("action_type" and its fields):
- click: click an element at "x", "y": integer pixel coordinates of the element's center \
on the {width}x{height} screenshot, (0, 0) being the top-left corner. Aim at the middle of \
the element, not its edge.
- double_click: like click, with a double click.
""" + _ACTIONS_COMMON

RULES = """\
Rules:
- Output exactly one action per response.
- Do exactly what the task asks. Fill in only the fields the task mentions and leave the \
others at their defaults. Don't create, change or delete anything else.
- Before each action, check on the screenshot whether your previous action had the expected \
effect. If it didn't, don't repeat it blindly: look for the reason and try another way.
- If the page looks incomplete or is still loading, use wait. If nothing changes after two \
waits in a row, act anyway.
- Pressing Enter in a single-line text field usually submits the form. Use "\\n" or Enter \
only when you mean to save; use Tab or a click to move to another field.
- If a dialog, popup or open dropdown covers what you need, deal with it first (choose an \
option, close it, or press Escape).
- Never reload the page (F5, Ctrl+R): it can submit a form again.
- Use the shift modifier only for Shift+Tab (previous field). Other Shift combinations \
(e.g. Ctrl+Shift+digit, Shift+F5) may not reach the application; capital letters go \
through type.
- History entries can carry feedback such as "grounding failed" or "invalid output". Take \
it into account in your next decision.
- DONE: only when the screenshot shows that the task is complete, e.g. the saved record \
with the requested values. If saving showed an error, fix it instead.
- FAIL: only when the task cannot be completed (e.g. information it needs is missing or the \
function doesn't exist). Explain why in "reasoning". One failed attempt is not a reason to \
give up.
"""

DEMO_SECTION = """\
<demo>
A demonstration of a similar task. Use it to learn where things are and in which order \
to do them. Don't copy its values: your task may ask for different ones.
{demo}
</demo>"""

RESPONSE_FORMAT = """\
Respond with one JSON object and nothing else, with the fields in this order:
{
  "observation": "<what the screenshot shows that matters for the task>",
  "previous_result": "<did your previous action have the expected effect? Write \
\\"first step\\" if there is no previous action>",
  "reasoning": "<what to do next and why it advances the task>",
  "decision": "COMMAND" | "DONE" | "FAIL",
  "action_type": "click" | "double_click" | "type" | "key" | "scroll" | "wait",
  ...the fields of the chosen action, as described under Actions
}
For DONE and FAIL leave out "action_type" and the action fields."""


class Planner:
    """Decides the next action and keeps the history of the current task.

    Args:
        model: VLM model name.
        provider: ``vlm_call`` provider, e.g. ``"anthropic"``.
        grounded: True when a grounder resolves ``target`` to coordinates; False when the
            planner outputs ``x``, ``y`` itself.
        environment: Environment section added to the system prompt (e.g.
            ``ENVIRONMENT_DOLIBARR``), or None.
        demo: Demo text included in every prompt, or None.
        max_history: Number of recent steps shown in the prompt.
        max_tokens: Response limit.
    """

    def __init__(
        self,
        model: str,
        provider: str = "anthropic",
        grounded: bool = True,
        environment: str | None = None,
        demo: str | None = None,
        max_history: int = 10,
        max_tokens: int = 1024,
    ):
        self.model = model
        self.provider = provider
        self.grounded = grounded
        self.environment = environment
        self.demo = demo
        self.max_history = max_history
        self.max_tokens = max_tokens
        self.history: list[StepRecord] = []

    def reset(self) -> None:
        """Forget the history (new task)."""
        self.history = []

    def plan(self, observation: BenchmarkObservation, task: BenchmarkTask) -> PlannerDecision:
        """Decide the next action. An unusable response becomes a ``wait`` with the reason in ``invalid``."""
        system, prompt = self._build_prompt(task, observation.viewport)
        raw = vlm_call(
            prompt,
            images=[observation.screenshot],
            system=system,
            model=self.model,
            provider=self.provider,
            max_tokens=self.max_tokens,
            cost_label="planner",
        )
        try:
            return self._parse(raw, observation.viewport)
        except ValueError as e:
            return self._invalid(raw, str(e))

    def record(self, decision: PlannerDecision, executed: str, feedback: str | None = None) -> None:
        """Append what actually happened to the history."""
        self.history.append(StepRecord(decision, executed, feedback))

    def add_feedback(self, feedback: str) -> None:
        """Attach feedback to the last step, e.g. an ``action_error`` reported one step late.

        Joined with "; " to existing feedback; ignored when there is no step yet.
        """
        if not self.history:
            return
        last = self.history[-1]
        last.feedback = f"{last.feedback}; {feedback}" if last.feedback else feedback

    def _build_prompt(self, task: BenchmarkTask, viewport: tuple[int, int]) -> tuple[str, str]:
        """Return (system, prompt).

        system: SYSTEM_PROMPT + environment + action space + RULES, formatted with ``viewport``.
        prompt: demo + task + history + RESPONSE_FORMAT.
        """
        width, height = viewport
        action_space = ACTION_SPACE_GROUNDED if self.grounded else ACTION_SPACE_COORDINATES
        sections = [SYSTEM_PROMPT, self.environment, action_space, RULES]
        system = "\n".join(s for s in sections if s).format(width=width, height=height)

        history = self._format_history() if self.history else "None: this is the first step."
        parts = []
        if self.demo:
            parts.append(DEMO_SECTION.format(demo=self.demo))
        parts.append(f"<task>\n{task.instruction}\n</task>")
        parts.append(f"<history>\n{history}\n</history>")
        parts.append("The attached screenshot shows the current screen. Decide the next action.")
        parts.append(RESPONSE_FORMAT)
        return system, "\n\n".join(parts)

    def _format_history(self) -> str:
        """Last ``max_history`` steps as numbered lines, e.g.
        ``3. click "New Third Party" -> click (70, 233)`` or
        ``4. click "Save" -> wait (grounding failed: no matching element)``.

        Numbers count from the start of the task. "-> executed" is shown only when it
        differs from the decision.
        """
        start = max(len(self.history) - self.max_history, 0)
        lines = [f"({start} earlier steps not shown)"] if start else []
        for number, step in enumerate(self.history[start:], start=start + 1):
            intent = _describe(step.decision)
            line = f"{number}. {intent}"
            if step.executed != intent:
                line += f" -> {step.executed}"
            if step.feedback:
                line += f" ({step.feedback})"
            lines.append(line)
        return "\n".join(lines)

    def _parse(self, raw: str, viewport: tuple[int, int] | None) -> PlannerDecision:
        """Turn the raw response into a validated decision.

        - The response must contain a JSON object with ``decision`` (COMMAND / DONE / FAIL).
        - ``observation``, ``previous_result``, ``reasoning`` are optional (missing -> "").
        - COMMAND needs a known ``action_type`` and its fields: grounded click ``target``,
          ungrounded click integer ``x``, ``y`` inside ``viewport``, type ``text``,
          key ``key`` (+ ``modifiers``), scroll ``scroll_direction``.
        - Case and surrounding spaces are ignored in ``decision``, ``action_type`` and
          ``scroll_direction``.

        Raises:
            ValueError: The response is unusable; the message says why.
        """
        response_dict = extract_json(raw)
        if not isinstance(response_dict, dict):
            raise ValueError("Planner couldn't parse VLM's answer to a JSON object.")

        if "decision" not in response_dict:
            raise ValueError("VLM didn't return 'decision' key.")

        decision = _normalized(response_dict["decision"]).upper()
        if decision not in get_args(Decision):
            raise ValueError(
                f"VLM returned 'decision' {response_dict['decision']!r}, "
                "not one of: 'COMMAND', 'DONE', 'FAIL'."
            )

        fields = {}
        if decision == "COMMAND":
            if "action_type" not in response_dict:
                raise ValueError("VLM returned COMMAND without 'action_type'.")
            fields = self._action_fields(response_dict, viewport)

        return PlannerDecision(
            decision=decision,
            reasoning=_optional_text(response_dict, "reasoning"),
            observation=_optional_text(response_dict, "observation"),
            previous_result=_optional_text(response_dict, "previous_result"),
            raw=raw,
            **fields,
        )

    def _action_fields(self, response: dict, viewport: tuple[int, int] | None) -> dict:
        """The validated fields of a COMMAND's action; fields of other actions are dropped."""
        action_type = _normalized(response["action_type"]).lower()
        if action_type not in get_args(ActionType):
            raise ValueError(f"unknown action_type {response['action_type']!r}")
        fields = {"action_type": action_type}

        if action_type in ("click", "double_click"):
            if self.grounded:
                fields["target"] = _required_text(response, "target", action_type)
            else:
                fields["x"], fields["y"] = _pixel(response, viewport)
        elif action_type == "type":
            fields["text"] = _required_text(response, "text", action_type)
        elif action_type == "key":
            fields["key"] = _required_text(response, "key", action_type)
            modifiers = response.get("modifiers") or []
            if not isinstance(modifiers, list) or not all(isinstance(m, str) for m in modifiers):
                raise ValueError("'modifiers' must be a list of strings")
            modifiers = [m.lower() for m in modifiers]
            unknown = [m for m in modifiers if m not in MODIFIERS]
            if unknown:
                raise ValueError(f"unknown modifiers {unknown}, allowed: {', '.join(MODIFIERS)}")
            fields["modifiers"] = modifiers or None
        elif action_type == "scroll":
            direction = _normalized(response.get("scroll_direction")).lower()
            if direction not in SCROLL_DIRECTIONS:
                raise ValueError(
                    f"scroll needs 'scroll_direction' 'up' or 'down', got {response.get('scroll_direction')!r}"
                )
            fields["scroll_direction"] = direction
        return fields

    @staticmethod
    def _invalid(raw: str, reason: str) -> PlannerDecision:
        """A ``wait`` decision that carries the reason, e.g. ``"invalid output: missing target"``."""
        return PlannerDecision(
            decision="COMMAND",
            reasoning="",
            action_type="wait",
            raw=raw,
            invalid=f"invalid output: {reason}",
        )


def _describe(decision: PlannerDecision) -> str:
    """The decision in one line, e.g. ``type "Acme\\n"``. Text and targets are JSON-quoted."""
    if decision.decision != "COMMAND":
        return decision.decision
    action = decision.action_type
    if action in ("click", "double_click"):
        if decision.target is not None:
            return f"{action} {json.dumps(decision.target)}"
        return f"{action} ({decision.x}, {decision.y})"
    if action == "type":
        return f"type {json.dumps(decision.text)}"
    if action == "key":
        return "key " + "+".join([*(decision.modifiers or []), decision.key])
    if action == "scroll":
        return f"scroll {decision.scroll_direction}"
    return str(action)


def _normalized(value) -> str:
    """A stripped string; anything but a string becomes ""."""
    return value.strip() if isinstance(value, str) else ""


def _optional_text(response: dict, field: str) -> str:
    """A free-text field: missing or null -> "", anything else as a string."""
    value = response.get(field)
    return "" if value is None else str(value)


def _required_text(response: dict, field: str, action_type: str) -> str:
    """A non-empty string field, returned unstripped."""
    value = response.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{action_type} needs a non-empty '{field}'")
    return value


def _pixel(response: dict, viewport: tuple[int, int] | None) -> tuple[int, int]:
    """Integer x, y on the screen; whole-number floats like 640.0 are accepted."""
    coords = []
    for field in ("x", "y"):
        value = response.get(field)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"click needs integer '{field}', got {value!r}")
        coords.append(value)
    x, y = coords
    if viewport is not None:
        width, height = viewport
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"({x}, {y}) is outside the {width}x{height} screenshot")
    elif x < 0 or y < 0:
        raise ValueError(f"({x}, {y}) is outside the screenshot")
    return x, y
