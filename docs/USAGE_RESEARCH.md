# Repo usage research — answers for `tests/loop_with_grounding.ipynb`

Research date: 2026-07-04. Scope: `openadapt-evals`, `openadapt-ml`, `openadapt-grounding`,
`openadapt-capture`, and the notebook `tests/loop_with_grounding.ipynb`.

---

## 1. Are you using the repo as intended?

**Mostly yes.** The composition you use is exactly the intended pattern:

```python
agent = PlannerGrounderAgent(
    planner="claude-sonnet-4-6", planner_provider="anthropic",
    grounder=OmniParserGrounder(url, selector_model=..., cache_dir=...),
)
```

`PlannerGrounderAgent` **is** the class that combines a planner and a grounder — you are not
using them "separately". The grounder is passed in as a `BenchmarkAgent` and the planner
calls it internally for click/double-click actions
(`openadapt_evals/agents/planner_grounder_agent.py:186`). There is no higher-level class
that would merge them further.

What you *did* hand-roll is the **run loop**. The framework's loop lives in
`openadapt_evals/benchmarks/runner.py` (`evaluate_agent_on_benchmark` → `_run_single_task`)
and does several things your notebook loop doesn't:

| Runner behavior | Your notebook loop |
|---|---|
| `agent.reset()` before each task (clears `_action_history`, pending actions) | never called → history leaks across runs unless you recreate the agent |
| `adapter.reset(task)` | never called (harmless for `LocalAdapter` — it just screenshots) |
| uses the observation returned by `adapter.step()` | discards it, calls `adapter.observe()` again (one extra screenshot per step) |
| respects `task.time_limit_steps` / `max_steps`, done/error handling | manual `range(10)` |
| **saves execution traces** (screenshots, actions, reasoning) via `ExecutionTraceCollector` | nothing persisted |
| live progress tracking, per-step timing, telemetry, `compute_metrics()` | nothing |
| optional done-gate (verify before accepting DONE) | n/a (needs a real `evaluate()`) |

**Caveat:** `evaluate_agent_on_benchmark` loads tasks via `adapter.list_tasks()` /
`adapter.load_task()`, and `LocalAdapter` has no task registry (`list_tasks()` returns `[]`,
`load_task()` raises). Two clean options:

1. Call the single-task runner directly (it's what everything else uses):

```python
from openadapt_evals.benchmarks.runner import _run_single_task, EvaluationConfig
from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector

config = EvaluationConfig(max_steps=10, model_id="sonnet-4-6+omniparser",
                          run_name="calc_test", output_dir="benchmark_results")
collector = ExecutionTraceCollector(benchmark_name="local", run_name=config.run_name,
                                    model_id=config.model_id, output_dir=config.output_dir)
result = _run_single_task(agent, adapter, task, config, collector)
collector.save_summary([result])
```

2. Or subclass `LocalAdapter` with a small task registry and use
   `evaluate_agent_on_benchmark` unchanged.

Note that `LocalAdapter.evaluate()` is a stub (always `score=0.0, success=False`) — results
will always read "failed" unless you plug in your own evaluator/VLM judge. The trace/viewer
machinery works regardless.

Notebook nits (cosmetic): `AgentPolicy`, `ApiVLMAdapter`, `StaticDatasetAdapter`,
`UIElement`, `BenchmarkAdapter`, `BenchmarkResult`, `BenchmarkObservation` are imported but
unused; `PlannerGrounderAgent` is imported twice; the `tunnel.stop()` cell sits *above* the
agent-construction cell that uses `tunnel.local_url`.

---

## 2. Adding user demonstrations

### Which class

Demonstrations are **not** part of `PlannerGrounderAgent`. The intended architecture is a
wrapper:

```python
from openadapt_evals.agents import DemoGuidedAgent, PlannerGrounderAgent
from openadapt_evals.demo_library import DemoLibrary

base = PlannerGrounderAgent(planner=..., grounder=OmniParserGrounder(...))
library = DemoLibrary("./demos")            # directory-based store
agent = DemoGuidedAgent(base_agent=base, demo_library=library)
```

`PlannerGrounderAgent` only exposes a `demo_guidance: str` attribute (an empty slot in its
planner prompt). `DemoGuidedAgent` fills it and additionally:

- looks up the demo **by `task.task_id`** (`DemoLibrary.get_demo(task_id)` — so the demo
  must be stored under the same task_id you put in `BenchmarkTask`),
- aligns your current screenshot to the closest demo step (pHash by default; `clip`,
  `hybrid`, `openai` strategies available; monotonic-progress bias; auto-disables after 3
  low-confidence alignments),
- injects a **plan overview** (all demo steps as a numbered strategy, coordinates stripped)
  plus a **per-step hint** (expected action / target / value) into the task instruction,
- optionally self-verifies each step against the demo's expected next screenshot
  (`enable_verification=True`, one extra VLM call per step),
- feeds the anti-loop recovery: when the planner repeats itself 3× it gets a demo-aware
  warning; after 6× identical actions the agent bypasses the planner and emits the first
  keyboard shortcut found in the demo.

For cross-task demo retrieval there is `RetrievalAugmentedAgent`, but it wraps `ApiAgent`
(not your planner-grounder stack) and uses a different `.txt` demo format + the separate
`openadapt-retrieval` package. For your setup, `DemoGuidedAgent` + `DemoLibrary` is the fit.

### Storage format

```
demos/
  {task_id}/
    {demo_id}/               # uuid4 hex, generated by add_demo
      demo.json              # Demo{task_id, demo_id, description, steps[], metadata}
      step_000.png           # one screenshot per step (state BEFORE the action)
      step_001.png
```

Each `DemoStep` = `{step_index, screenshot_path, action_type, action_description,
target_description, action_value, x, y, description}`. Created via:

```python
library.add_demo(
    task_id="123",
    screenshots=[png_bytes_or_paths, ...],   # one per action
    actions=[BenchmarkAction(type="click", x=0.5, y=0.3), ...],
    description="Calculate final price using DATA file",
    resolution=(2560, 1440),                 # your monitor — enables coord normalization
)
library.enrich_demo("123", provider="anthropic", model="claude-haiku-4-5-20251001")
```

`enrich_demo()` is worth running: it VLM-describes each click target ("three-dot menu in
Chrome toolbar") so the guidance reads as natural language instead of `CLICK(0.96, 0.07)`.

### How to create demos — your options

1. **From your own `openadapt-capture` recordings (recommended).** Your `start_clock/`
   dirs already contain everything needed: `recording.db` (SQLite with `ActionEvent`,
   `Screenshot`, `WindowEvent` tables) and `screenshots/capture_*_step_*.png`.
   openadapt-capture's processing layer already merges raw events into semantic
   `MouseClickEvent` / `MouseDoubleClickEvent` / `KeyTypeEvent` / `MouseDragEvent`
   (`openadapt_capture/events.py`, `processing.py`). **There is no converter in the repo
   yet** — the existing `scripts/convert_recording_to_demo.py` converts WAA-VM recordings
   to the *text* demo format for `ApiAgent`, not to `DemoLibrary`. A small script
   (~50 lines) mapping capture events → `BenchmarkAction` + the per-step screenshot →
   `library.add_demo(...)` is the missing piece. Record on the same monitor you run the
   agent on (monitor_index=1) and pass `resolution=` so coordinates normalize.

2. **Manual demos without screenshots** — `openadapt-evals/scripts/create_manual_demo.py`
   authors a `demo.json` from lines like `key:win+r | Press Win+R to open Run dialog`.
   Without screenshots the library falls back to sequential step alignment, which is fine:
   descriptions carry the guidance.

3. `record_waa_demos.py` / `record_demo_screenshots.py` — WAA-VM based, not relevant for
   your local setup.

### Should the grounding module be part of the demos?

**No, for your use case.** The design deliberately splits roles:

- Demos guide the **planner** (what to do, in what order, which shortcuts). The plan
  overview intentionally strips coordinates ("decide click targets based on what you
  actually see, not memorized coordinates").
- Grounding stays a **live, per-step** concern: the planner's `target_description` goes to
  your `OmniParserGrounder` at inference time, demo or no demo.

So keep recording plain screenshots + actions; do not run OmniParser during demo recording.

There *is* an advanced mode where grounding data is baked into demos: the **grounding
cascade** (`openadapt_evals/grounding.py` `GroundingTarget`, `scripts/enrich_demo_targets.py`,
`docs/design/grounding_cascade_design_v3.md`) used by **`DemoExecutor`**
(`agents/demo_executor.py`). That's a different execution model: the demo is *replayed* as a
script (keyboard/type steps deterministic, clicks grounded from stored crops/nearby-text,
planner only as recovery). Use it if you later want "replay my demo robustly" rather than
"let the planner improvise with demo hints".

---

## 3. Is run history persisted in the prompts?

**Yes — as text, inside `PlannerGrounderAgent`.** Every `act()` appends a line to
`self._action_history`, e.g.:

```
CLICK(0.723, 0.562) (instruction: 1 button on the calculator)
KEY(win+r) (queued)
```

The last `max_history` (default 10) lines are rendered into the `Previous actions:` section
of the planner prompt. So at step 2 the LLM does know what it did at step 1.

What the LLM does **not** get:

- past screenshots — only the *current* screenshot is attached (each planner call is a
  fresh, stateless single-turn `vlm_call`; there is no chat/messages thread),
- outcomes/results of past actions — only the action strings, so it can't tell that a click
  landed on the wrong element (your calculator log shows exactly this: the grounder kept
  resolving "1 button" to the "8" key, and the planner only inferred it from the current
  screenshot showing an unexpected "8"),
- grounder context — `OmniParserGrounder` gets *no* history at all, just the current
  screenshot + one instruction.

The mitigations for the no-feedback blind spot are the anti-loop warning (3 identical
instructions → warning injected into the prompt) and the forced demo-shortcut override (6
identical → planner bypassed).

The `history` parameter of `act(obs, task, history)` (list of (observation, action) tuples
that the runner passes) is accepted but **ignored** by `PlannerGrounderAgent` — its internal
`_action_history` is the only memory.

---

## 4. What does the prompt look like, and how does it evolve?

Per step, the planner receives (all from `planner_grounder_agent.py:86-162`):

**System:** "You are a desktop automation planner. You MUST follow the task instruction
exactly... Do NOT open or click on applications that the task does not ask you to use."

**User message** (+ current screenshot as image):

```
=== YOUR TASK (follow this EXACTLY) ===
{task.instruction}                        ← constant (unless done-gate/demo augments it)
=== END TASK ===

IMPORTANT: Every action you take MUST work toward completing the task above. ...

Previous actions:
  1. DOUBLE_CLICK(0.804, 0.601) (instruction: DATA file icon on the desktop)
  2. KEY(win+r) (instruction: ...)        ← GROWS each step, sliding window of 10
  ...

Accessibility tree:
(not available)                           ← always this with LocalAdapter (no a11y capture)

{demo_guidance}                           ← "" unless DemoGuidedAgent sets it
Look at the screenshot and decide the next action to advance the TASK above.

Output a JSON object with exactly these fields:
{"decision": "COMMAND" | "DONE" | "FAIL", "action_type": ..., "action_value": ...,
 "target_description": ..., "reasoning": ...}

Rules: ...(static rules)...
{anti_loop_warning}                       ← "" until 3 identical instructions in a row
```

Then, for click/double-click decisions only, a **second** model call happens — in your setup
that's `OmniParserGrounder._select_element`: OmniParser's SOM-annotated screenshot + a list
of `index: label` lines + "Which element index should be clicked... Reply with only the
integer index." (`type`/`key`/`scroll` actions never touch the grounder.)

**What changes step to step:** only `{action_history}` (grows, then slides), and
`{anti_loop_warning}` (appears/disappears). Everything else is static per task. With
`DemoGuidedAgent`, the task-instruction block additionally carries the plan overview and a
per-step aligned hint, so it changes each step too. With the runner's `done_gate=True`, a
rejected DONE appends `[SYSTEM: The task is NOT yet complete...]` to the instruction.

**"Restarting" the prompt** = clearing that internal state:

- `agent.reset()` — clears `_action_history`, `_pending_actions`, `_step_index`, and
  recursively resets planner/grounder agents. The runner does this before each task.
- Recreating the agent object (what you effectively do when you re-run the notebook cell).
- Changing `task_id` alone does **not** reset `PlannerGrounderAgent` (unlike
  `DemoGuidedAgent`, which detects task changes itself). If you start a new task without
  `reset()`, the old actions bleed into the new prompt.

---

## 5. Documenting & visualizing a full run

Nearly all of this already exists in the repo; it's just not wired into your notebook loop.

### Recommended stack (no new code)

1. **Execution traces** — run through `_run_single_task` with an `ExecutionTraceCollector`
   (see §1 snippet). You get, per run:

   ```
   benchmark_results/{run_name}/
     metadata.json                       # model_id, timestamps
     summary.json
     tasks/{task_id}/
       task.json                         # instruction, domain
       execution.json                    # per step: serialized action incl. raw_action →
                                         #   planner_output {decision, action_type,
                                         #   target_description, reasoning} and the
                                         #   grounder's {element_index, element_text,
                                         #   element_bounds}; timing; captured log lines
       screenshots/step_000.png ...      # exactly what the agent saw at each step
   ```

2. **HTML viewer** — `openadapt-evals view --run-name {run_name}` (or
   `generate_benchmark_viewer()` from Python) renders a standalone `viewer.html`:
   task list with pass/fail, step-by-step replay with screenshots, actions, reasoning, and
   the captured execution log. This is your "what did the agent see and think" UI.

3. **Exact planner inputs/outputs** — attach the SFT logger:

   ```python
   from openadapt_evals.training.trajectory_logger import PlannerTrajectoryLogger
   agent = PlannerGrounderAgent(..., trajectory_logger=PlannerTrajectoryLogger("./traj", keep_failed=True))
   ```

   Writes `trajectories.jsonl` + per-episode PNGs with the full planner input (task
   instruction, action history, a11y) and parsed output per step. Use `keep_failed=True`
   — by default `end_episode(reward<=0)` deletes failed episodes.

4. **Grounder-side artifacts** — you already have this: `OmniParserGrounder(cache_dir=...)`
   saves the SOM-annotated PNG + element JSON per parse.

5. **Exact rendered prompt strings** — the one gap. Nothing persists the final formatted
   prompt text locally (it's reconstructible from the template + trajectory logger fields).
   Two options:
   - `weave_init("openadapt-evals")` (`integrations/weave_integration.py`) — `vlm_call`,
     `act` and `_call_planner` are already `@weave_op`-decorated, so every call (prompt,
     images, response, latency, cost) lands in the W&B Weave trace UI. Needs a wandb
     account + `pip install weave`.
   - or (future code change, ~5 lines) log `prompt` in `_call_planner` at INFO / write it
     into the trajectory logger record.

6. **Aggregates & cost** — `python -m openadapt_evals.analysis <traces> --report report.html`
   (`TraceAnalyzer` reads both trajectory-logger JSONL and viewer-format dirs; failure-mode
   classification, run-to-run comparison). `from openadapt_evals.cost_tracker import
   get_cost_tracker; print(get_cost_tracker().summary_text())` — every `vlm_call` is already
   tracked with your `cost_label`s (`planner`, `omniparser_grounder`, ...).

### Suggested notebook flow

```python
# once per run
config = EvaluationConfig(max_steps=15, model_id="sonnet46+omniparser",
                          run_name=f"run_{time.strftime('%Y%m%d_%H%M%S')}",
                          save_execution_traces=True, enable_live_tracking=False)
collector = ExecutionTraceCollector("local", config.run_name, config.model_id)
result = _run_single_task(agent, adapter, task, config, collector)
collector.save_summary([result])

# then
# openadapt-evals view --run-name run_... --benchmark-dir benchmark_results
```
