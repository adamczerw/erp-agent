# erp-agent: Plan

Goal: a GUI agent that automates the Dolibarr ERP, evaluated through openadapt-evals.
The agent runs on the host (WSL). Screenshots and input happen inside the sandbox
container's virtual desktop, over VNC.

> Working style: I'm building this to learn. Help with guidance, explanations and
> review, but let me write the implementation myself unless I ask for code.

## Environment (already exists)

- `../environment/opensandbox/`: sandbox container started via OpenSandbox (`main.py`).
  It runs Xvfb `:0` at **1280x800**, XFCE, Chrome, **x11vnc on 5900** (password auth,
  `-shared`) and websockify/noVNC on 6080.
- `../environment/dolibarr/`: docker compose with `web` (Dolibarr, host port 80) and `mariadb`.
- `../environment/bench/`: `reset.sh`, `checkpoint.sh`, `snapshot.sh`, `config.sh`.
  These restore the DB and documents to a baseline and compare table row counts.
- `../openadapt-all/`: my OpenAdapt forks. This package depends on them; they don't depend on it.

## Decisions

- **My work lives in erp-agent, not in the forks.** The forks hold only upstream code plus
  my committed fixes. `OmniParserGrounder` moved here, and my planner prompt fix was
  reverted in the fork (adamczerw/openadapt-evals#2).
- **Control channel is VNC (the RFB protocol).** No shell commands and no
  accessibility tree. Pure screenshots plus pointer and key events.
- **Settle detection uses incremental framebuffer updates.** After an action:
  1. Wait for the first change (~1.5 s timeout).
  2. Then wait for a quiet period (~300–700 ms).
  3. Stop at a hard cap (~5–10 s).
  4. Ignore tiny changes (the blinking caret) and mask noisy regions.
  5. "No update arrived" is the quiet signal: the server holds incremental requests.
- **Evaluation is ground truth from the DB** (SQL checks), not screenshot judging.
- **Own agent instead of extending `PlannerGrounderAgent`.** Context management will grow,
  and its "planner as an agent" path loses information: no structured fields, no FAIL,
  no reasoning, and no feedback about what was actually executed. So `ErpAgent` is my own
  orchestrator. Upstream `PlannerGrounderAgent` + `OmniParserGrounder` is the **baseline**.

## Architecture

```
run.py ──► evaluate_agent_on_benchmark(agent, adapter, task_ids)     # openadapt-evals runner
             agent   = ErpAgent(planner=..., grounder=OmniParserGrounder(...))
                       (baseline: upstream PlannerGrounderAgent with the same grounder)
             adapter = DolibarrAdapter(desktop=VncDesktop(...),
                                       backend=Dolibarr(...),         # optional
                                       tasks_dir=...)
```

| Component | File | Responsibility | Knows about |
|---|---|---|---|
| planner | `src/erp_agent/agent/planner.py` | prompt, history, memory: all context management | the task, the agent's feedback |
| `OmniParserGrounder` | `src/erp_agent/agent/grounding.py` | target description → normalized x,y (`BenchmarkAgent` interface) | OmniParser, a VLM |
| `ErpAgent(BenchmarkAgent)` | `src/erp_agent/agent/agent.py` | planner → (grounder if click) → `BenchmarkAction`; tells the planner what actually executed and what failed | planner and grounder |
| `VncDesktop` | `src/erp_agent/desktop/vnc.py` | RFB connection, framebuffer → PNG, click/type/key/scroll/drag, settle detection | VNC only |
| `Dolibarr` | `src/erp_agent/dolibarr/backend.py` | reset DB and documents (bench scripts path passed in), run SQL queries | backend only |
| `DolibarrAdapter(BenchmarkAdapter)` | `src/erp_agent/dolibarr/adapter.py` | `list_tasks`, `load_task`, `reset`, `step`, `evaluate`, `close`; composes the two | both |
| `DesktopSandbox` (later) | `src/erp_agent/sandbox.py` | start and stop the sandbox container, wait for VNC readiness | OpenSandbox |

- `ErpAgent` turns planner JSON into actions:
  - `type` text ending in `\n` → type the text, then queue an Enter
  - `"Ctrl+A"` → key + modifiers
  - click and double_click → grounder
  - DONE and FAIL → `done` and `error`
- `reset(task)`:
  1. `backend.reset_state()`
  2. Navigate the browser to `task.start_url` with `VncDesktop` (ctrl+L, type URL, Enter).
  3. Log in if the login page appears.
  4. Release any stuck modifier keys.
  5. Return the first screenshot.
- `step(action)`: convert normalized coordinates to 1280x800, send RFB events, wait for
  the screen to settle, return `(obs, done, info)`.
- `evaluate(task)`: run the task's SQL check through `backend` and return a `BenchmarkResult`.
- Tasks are data (JSON in `tasks/dolibarr/`): instruction, start URL, SQL success check.
  Put task-specific data in `BenchmarkTask.raw_config`. Don't add per-task methods to `Dolibarr`.
- Without a `backend`, reset only takes a screenshot and evaluate returns "not
  evaluated", so the same adapter works for real automation.

## Reference code (`../openadapt-all/openadapt-evals/openadapt_evals/`)

- `adapters/base.py`: `BenchmarkAdapter`, `BenchmarkAction`, `BenchmarkObservation`, `BenchmarkTask`, `BenchmarkResult`
- `adapters/local/adapter.py`: closest model for `step`/action dispatch (`_execute_action`, `_do_click`, `_to_pixels`, `observe`)
- `adapters/waa/live.py`: remote-environment model (agent local, env remote, real `reset`/`evaluate`)
- `benchmarks/runner.py`: `evaluate_agent_on_benchmark` (loop: `agent.act` → `adapter.step` → `adapter.evaluate`)
- `agents/planner_grounder_agent.py`: the baseline.
  - `_build_action_from_structured` and `_call_grounder` are a useful reference for `ErpAgent`.
  - The grounder outputs **normalized 0–1** coordinates.
  - Its prompt assumes Windows (Start menu, Win+R).
- `infrastructure/screen_stability.py`: WAA's screenshot-diff settle detection (99.5% similarity threshold).

## Build order

1. **Connectivity.** Connect a normal VNC viewer (TigerVNC) to the sandbox's 5900
   endpoint. If the OpenSandbox proxy blocks raw TCP, use RFB over WebSocket on 6080.
   Check that Chrome in the sandbox can reach Dolibarr (host IP or a shared Docker
   network, not `localhost`; `DOLI_URL_ROOT`).
2. **Screenshot.** Handshake, then a full update, then save a PNG.
3. **Click.** `PointerEvent` press, then release.
4. **Typing.** Keysyms with modifiers. Test the string `Zażółć "gęślą" $jaźń`; the
   fallback is `ClientCutText` + ctrl+v.
5. **Settle detection.**
6. **`Dolibarr` backend**, then **`DolibarrAdapter`**: `step`/`observe` → `reset` →
   `evaluate` → `list_tasks`/`load_task`.
7. **1–3 simple tasks** in `tasks/dolibarr/` (e.g. create a customer). Check that a
   hand-written sequence of actions completes one through the adapter.
8. **Baseline run:** `run.py` with upstream `PlannerGrounderAgent` + `OmniParserGrounder`
   through `evaluate_agent_on_benchmark`, with a VNC viewer attached to watch.
9. **`ErpAgent`**: own planner (reuse the "precise target description" wording) plus the
   orchestrator. Compare against the baseline.
10. **Context management** in the planner (reasoning carry-over, earlier screenshots,
    history summaries…), measured against the baseline.
11. **Later:** `DesktopSandbox` for one-command runs.

Library options: vncdotool, asyncvnc, or my own minimal RFB client (handshake, VNC auth, Raw encoding, pointer and key events).

## Gotchas

- **VNC auth:** DES with the password as key, and the bits of each key byte reversed.
- **Click:** button mask down, then up at the same x,y. Scroll is buttons 4/5. Drag
  holds the mask while moving.
- **Cursor:** drawn into the framebuffer, or sent separately via the cursor
  pseudo-encoding. Decide deliberately.
- **Coordinates:** VNC maps 1:1 to Xvfb (no HiDPI or monitor offsets, unlike LocalAdapter).
- **DB reset:** may invalidate the browser session, so handle the login page in `reset`.
- **Sync vs async:** the OpenSandbox SDK is async; `BenchmarkAdapter` and the runner are sync.

## Setup

See `README.md`. In short:
- Shared venv in `../.venv`.
- Forks installed with `uv pip install --no-sources -e ...`.
- This package installed with `uv pip install --no-deps -e .`.

The forks carry three deliberate uncommitted workarounds:
- `openadapt-evals/pyproject.toml`
- `openadapt-ml/pyproject.toml`
- `openadapt-grounding/uv.lock`

Copies are in `C:\Users\AdamCzerwonski\other\magisterka\code\to-erp-agent\workarounds\`.

Use `wsl -d Ubuntu`: the default distro is `docker-desktop`.
