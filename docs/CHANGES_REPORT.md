# Local changes across the OpenAdapt repos

*Audit date: 2026-09-26. Covers the 5 git repos in `openadapt-all/`.*

## TL;DR

- **Nothing has been committed or pushed.** Every repo is on `main`, level with `origin/main` (your forks at `github.com/adamczerw/...`). The reflog of each repo has one entry, the original `clone`. There are no stashes and no other branches.
- **All of your work exists only as uncommitted changes in the working trees.** File timestamps show it was done between 2026-05-09 and 2026-07-18.
- The real changes cover four areas:
  1. **Multi-monitor recording** in `openadapt-capture` (plus the `openadapt` CLI wrapper).
  2. **An OmniParser-based grounder** and multi-monitor action execution in `openadapt-evals`.
  3. **A hardened AWS deployment for OmniParser** in `openadapt-grounding`: SSH tunnel instead of a public port, a CUDA 13 image, image caching, and streamed build logs.
  4. **Fixes for broken imports and calls** in `openadapt-ml` and `openadapt`.
- Some changes are noise: a `CLAUDE.md` rename in every repo, and executable-bit flips on 10 files.
- I found **2 bugs worth fixing before you commit** (see [Issues found](#issues-found)).

| Repo | Base commit (upstream) | Modified | New (untracked) | What it's about |
|---|---|---|---|---|
| `openadapt-capture` | `a7bfb54` release 0.5.2 | 6 | – | Monitor selection, strip Ctrl×3 stop keys, shutdown fix |
| `OpenAdapt` | `e4d4d03` 1.2.2 | 1 (+7 mode-only) | – | `--monitor` flag, fixed `train`/`serve` wiring |
| `openadapt-evals` | `3defa5b` release 0.87.0 | 6 | `omniparser_grounder.py` | OmniParser grounder, multi-monitor clicks, model compat |
| `openadapt-grounding` | `677a146` fix(ci) | 7 | `check_server.py` | AWS deploy: tunnel, CUDA 13, caching, streaming logs |
| `openadapt-ml` | `fecf461` fix: max_new_tokens | 5 (+3 mode-only) | `my_docs/` | Restored missing functions, dependency changes |

In each row, `CLAUDE.md` deleted + `CLAUDExxx.md` untracked is also present; it is not counted above.

---

## Noise that appears in every repo

### `CLAUDE.md` → `CLAUDExxx.md`
In all 5 repos, `CLAUDE.md` was renamed to `CLAUDExxx.md`, most likely to stop Claude Code from auto-loading it. The content is identical; in `openadapt-evals` and `openadapt-grounding` only the line endings differ. Git sees this as a deletion plus an untracked file.

**Decide:** either restore it (`git checkout -- CLAUDE.md && rm CLAUDExxx.md`) or keep the rename local and never commit it.

### Executable-bit flips (content unchanged)
These files show `mode change 100755 => 100644` with 0 lines changed:

- `OpenAdapt`: `docs/_scripts/generate_docs_screenshots.py`, `legacy/build_scripts/{macos.sh,test_app_run_macos.sh,upload_release_artifacts.sh}`, `legacy/openadapt/app/dashboard/{entrypoint.ps1,entrypoint.sh}`, `legacy/scripts/postinstall.py`
- `openadapt-ml`: `scripts/{check_azure_resources.sh,waa_bootstrap_helper.sh,waa_bootstrap_local.sh}`

This is a Windows artifact: those two repos have `core.fileMode=true`. Fix it with `git config core.fileMode false` in both. The files will then drop out of `git status`.

---

## openadapt-capture

### 1. Monitor selection (new feature)
Before this change, recording always captured `mss.monitors[0]`, the virtual screen spanning all monitors. Now one physical monitor can be chosen.

- **`config.py`**: new setting `MONITOR_INDEX: int = 0` (0 = all combined, 1 = primary, 2 = secondary, …). It is wired into `RecordingConfig.monitor_index` and `_FIELD_TO_CONFIG_ATTR`.
- **`utils.py`**:
  - New `list_monitors()` and `get_monitor_info(monitor_index=None)`. If the index is out of range, it falls back to 0 with a warning.
  - `get_monitor_dims()` and `take_screenshot()` take an optional `monitor_index` and default to `config.MONITOR_INDEX`.
- **`input.py`**: `ScreenCapturer` gets a `monitor_index` parameter.
- **`recorder.py`**:
  - New `_translate_coords()`. When a single monitor is selected, mouse move/click/scroll events **outside** it are dropped, and events inside it are shifted so that `(0, 0)` is the monitor's top-left corner.
  - `read_mouse_events()` computes `monitor_bounds` once and passes it to the listeners through `partial`.
  - `Recorder.__init__()` accepts `monitor_index=`.
  - Removed the import-time `monitor_width, monitor_height = utils.take_screenshot().size`, which grabbed a screenshot as soon as the module loaded. The video writer now uses `recording.monitor_width/height`, which already respects the selected monitor.
- **`cli.py`**:
  - `capture record ... --monitor N`, with a range check and the list of monitors printed when there is more than one.
  - New `capture monitors` command.

### 2. Strip the stop sequence from the end of recordings
**`capture.py`**: new `_strip_trailing_stop_sequence()`. When a recording is stopped with a keyboard sequence (for example Ctrl×3), those key events are no longer returned by `CaptureSession`'s event list. They were meta-actions that would confuse an agent replaying the demo. The function matches on canonical key names against `config.STOP_SEQUENCES`.

### 3. Shutdown fix
**`recorder.py` → `process_events()`**: `event_q.get()` became `event_q.get(timeout=0.1)` with a `queue.Empty` → `continue`. The loop can now notice `terminate_processing` even when the queue is empty, so shutdown can no longer hang on a blocking `get()`.

---

## OpenAdapt (umbrella CLI, `openadapt/cli.py`)

- **`openadapt capture start --monitor/-m N`**: mirrors the capture CLI (lists monitors, validates the index, passes `monitor_index` to `Recorder`).
- **`openadapt train start`**:
  - Fixed the import (`from openadapt_ml.scripts.train import main as train_main`; `train_main` didn't exist).
  - Fixed the keyword arguments (`capture_path=`, `config_path=`).
  - Side effect: the `--model` option is now printed but **not passed** to training, so it does nothing.
- **`openadapt serve`**: the import was renamed from `serve_dashboard` (which doesn't exist) to `cmd_serve`, **but the call is still broken**. See [Issues found](#issues-found).

---

## openadapt-evals

### New file: `openadapt_evals/agents/omniparser_grounder.py`
`OmniParserGrounder` is a `BenchmarkAgent` that plugs into `PlannerGrounderAgent` as the `grounder`. For each step:

1. It sends the screenshot to the OmniParser server (`OmniParserClient.parse_with_metadata`) and gets back the detected elements plus a set-of-marks (SOM) image.
2. It asks a VLM (default `claude-haiku-4-5-20251001`) to pick the element index that matches the planner's `target_description`. The model gets the numbered list and the SOM image, and may reply `-1` for "no match". An invalid reply is retried once with a correction prompt.
3. It returns a `click` at the element's normalized center.

If grounding fails, it returns a no-op `wait` action instead of `done`, so the episode continues and the planner looks again. With `cache_dir`, it saves each SOM image and element list as `{timestamp}.png/.json` for debugging.

### `adapters/local/adapter.py`: executing actions on a chosen monitor
- `_to_logical()` now adds the selected monitor's `left/top` offset before dividing by the DPI scale, so clicks land on the right monitor.
- New `_to_pixels()`: if both `x` and `y` are in `[0, 1]`, they are treated as normalized and scaled by the monitor's width and height. Otherwise they are treated as pixels. It is applied in `_do_click` and to drag start and end points.
- `"win"` and `"windows"` are added as aliases for `Key.cmd`.

### Other changes
- **`agents/api_agent.py`**: removed the normalization of `click(x, y)` by viewport, so `ApiAgent` now emits raw pixels. The adapter's `_to_pixels()` handles both forms.
- **`agents/planner_grounder_agent.py`**:
  - The planner prompt asks for a more precise `target_description` (exact label, or where to click to activate a window).
  - The default `decision` is now `""` instead of `"COMMAND"`. Behaviour is unchanged, because anything other than DONE or FAIL still runs as a command.
  - The log line now includes `action_type` and `action_value`, and the separate "Planner task instruction" log was removed.
- **`benchmarks/viewer.py`**: `write_text(html, encoding="utf-8")`. This fixes a `UnicodeEncodeError` on Windows, where the default code page is used otherwise.
- **`vlm.py`**: `temperature` is no longer sent to model IDs containing `opus-4-7`, `opus-4-8`, `sonnet-5` or `fable`, because those models return a 400 when it is sent.
- **`pyproject.toml`**: the `openadapt-types>=0.1.0` dependency is commented out. No code in the repo imports `openadapt_types`.

---

## openadapt-grounding (OmniParser on AWS)

### Security: the API is no longer exposed publicly
- The security group now opens **only port 22**. The OmniParser API port is reached through an SSH tunnel.
- **New `deploy secure` command**: revokes `0.0.0.0/0` on the API port for existing deployments.
- **New `deploy tunnel` command**:
  - Runs `ssh -L <port>:localhost:<port> -N ...` and waits until the local port accepts connections.
  - From the CLI (`block=True`), it keeps the tunnel open until Ctrl+C.
  - From Python, it returns a new `TunnelProcess` object. It works as a context manager: `with Deploy.tunnel() as url: ...`.
- The `status` command prints the ready-made tunnel command and `http://localhost:<port>` instead of a public URL.

### Docker image (`deploy/Dockerfile`)
- Base image changed from `nvidia/cuda:12.3.1` to `nvidia/cuda:13.0.3-devel-ubuntu22.04`.
- Added `torch==2.11.0` and `torchvision` from the `cu130` index.
- `flash-attn` is no longer compiled from source. It is installed from a **prebuilt third-party wheel** (`github.com/adithyaxx/flash-attention` v2.8.3, cu13, torch 2.11, cp312).
- `sed` switches the OmniParser server to `reload=False`, so EasyOCR initialises in the main process with a GPU context.

### Deploy workflow (`deploy/deploy.py`)
- **Instance setup**:
  - Installs the NVIDIA Container Toolkit and configures the docker runtime.
  - Removed `docker system prune -af --volumes`, so cached images survive.
  - Prints the docker images present on the instance.
- **Image caching**:
  - The clone and build are skipped when `docker image inspect <image>` succeeds.
  - `docker rmi` is no longer run before a build.
- **`execute_command()` rewrite**:
  - Uses `get_pty=True`, so `docker build` output streams live.
  - Reads 64 KB chunks and strips ANSI codes and carriage returns.
  - Prints a heartbeat every 60 s and aborts after 1 h without output. This replaces the fixed `COMMAND_TIMEOUT`.
- **SSH robustness**:
  - `set_keepalive(30)` on both SSH connections.
  - `chmod 600` before deleting an old `.pem`; read-only files fail to delete on Windows.
  - Fixed the `ssh -tt` argument order.
  - `_run_ssh_command()` decodes output as UTF-8 with `errors="replace"`.
- **Logs**: `docker logs --timestamps`, with the timestamps converted to **Europe/Warsaw** time (`_localize_timestamps`).
- **New `deploy pause` command**: stops the EC2 instance without terminating it.

### Config (`deploy/config.py`)
- `.env` discovery is cached and logs which file it used.
  - Removed a dead duplicate check.
- The `.pem` key path is now absolute: it resolves to the directory that holds `.env` instead of `./`. This is the same for `DeploySettings` and `UITarsSettings`.
- AWS credentials from `.env` are copied into `os.environ` (`setdefault`) so that boto3 finds them.

### Other changes
- **`deploy/uitars/deploy.py`** gets the same subset: streaming `execute_command`, keepalive, `chmod` before delete, image caching, the `-tt` fix, UTF-8 decoding and `pause`. The tunnel/secure changes are **not** ported here; UI-TARS still opens its port publicly.
- **`parsers/omniparser.py`**: `confidence` now defaults to `None` instead of `1.0` when the server doesn't return one.
- **`uv.lock`**: regenerated by a newer uv. The lock format is now `revision = 3`, and the package's own version went from 0.1.0 to 0.1.2. No dependencies were added or removed.
- **`check_server.py`** (untracked): a 3-line scratch script that greps the remote server file for `reload`. Probably delete it.

---

## openadapt-ml

These changes mostly make the upstream code importable and runnable. Upstream referenced functions that didn't exist.

- **`ingest/synthetic.py`** and **`ingest/__init__.py`**: added a `Session` dataclass and `generate_synthetic_sessions()` as a wrapper around `generate_synthetic_episodes()`, one episode per session. `scripts/demo_policy.py` imports this function; it was missing upstream.
- **`training/trainer.py`**: added `update_current_symlink_to_latest()`, which atomically repoints `training_output/current` to the newest run. `cloud/local.py::cmd_serve` imports it; it was missing upstream. On Windows, creating symlinks requires Developer Mode or admin rights.
- **`scripts/train.py`**: `capture_to_episode(..., goal=goal)` became `instruction=goal`, because the function has no `goal` parameter.
- **`pyproject.toml`**:
  - Commented out the `openadapt-capture>=0.3.0` dependency and its `[tool.uv.sources]` editable path. You presumably install the local capture repo yourself.
  - Added unpinned `datasets`, `trl` and `huggingface_hub`.
- **`my_docs/overview.md`** (untracked): a module-tree overview of `openadapt_ml/`.

---

## Issues found

1. **`openadapt serve` still crashes** (`OpenAdapt/openadapt/cli.py`, around line 427).
   - `cmd_serve` is declared as `def cmd_serve(args: argparse.Namespace) -> int`, but the CLI calls it with keyword arguments (`output_dir=`, `port=`, `open_browser=`). That raises a `TypeError`.
   - It reads `args.port`, `args.benchmark`, `args.no_regenerate`, `args.start_page` and `args.quiet`. There is no `output_dir` or `open_browser`, so you'd need to build an `argparse.Namespace(port=port, benchmark=..., no_regenerate=False, start_page=None, quiet=False)` and check how it chooses the output directory.
2. **The planner prompt shows invalid JSON** (`openadapt-evals/.../planner_grounder_agent.py:123`).
   - The edited `"target_description": "<...>"` line lost its trailing comma before `"reasoning"`.
   - If the model copies the template, `extract_json()` fails and the code falls back to `{"decision": "COMMAND", "instruction": <entire raw reply>}`. That means one wrong action per step. Add the comma back.

Smaller things to be aware of:

- **Out-of-range pixels in `_to_pixels`**: a pixel coordinate of exactly `(0,0)`–`(1,1)` would be treated as normalized. In practice this is harmless.
- **`mss` opened for every action**: `_to_logical` and `_to_pixels` open a new `mss.mss()` on every call.
- **Indentation in `_to_logical`**: it is indented 12 spaces instead of 8 and has trailing whitespace. This is valid Python, just inconsistent.
- **Hard-coded model list in `vlm.py`**: the no-`temperature` list is a fixed set of substrings. A new model ID that also rejects `temperature` will need to be added to it.
- **`confidence=None` in grounding**: this goes against `Element.confidence: float = 1.0`. Anything that sorts or thresholds by confidence will break when the server doesn't return scores.
- **Image caching means Dockerfile edits are ignored**: once the image exists on the instance, Dockerfile edits don't take effect until you run `sudo docker rmi omniparser` there manually.
- **The docker build context is now the whole home directory**: `cd OmniParser &&` was dropped from the build command, so the build runs in `~`. This works because the Dockerfile clones OmniParser itself, but the whole home directory is sent as the build context.
- **Third-party flash-attn wheel**: it comes from a personal GitHub release (`adithyaxx`), not an official one. This is a supply-chain trust decision.
- **Public IP in a docstring**: the `OmniParserGrounder` docstring example contains a real public IP (`54.156.76.227`). Replace it with a placeholder before pushing.

---

## Outside the repos

The parent folder `openadapt-all/` is **not** a git repo. So these are not version-controlled or backed up anywhere:

- `USAGE_RESEARCH.md`
- `demo_invoice/`, `start_clock/`, `start_clock2/`, `start_clock3/`, `synthetic/`, `test/`, `tests/`
- `.env` and `omniparser-key.pem`: secrets. Keep them out of every repo.

---

## Suggested next steps

1. Fix the two issues above.
2. Remove the noise:
   - Run `git config core.fileMode false` in `OpenAdapt` and `openadapt-ml`.
   - Decide what to do about the `CLAUDE.md` rename.
   - Delete `check_server.py`.
3. Commit each repo on a feature branch, then push to your forks. Suggested grouping:
   - `openadapt-capture`: (a) monitor selection, (b) strip stop sequence, (c) `process_events` queue timeout.
   - `OpenAdapt`: CLI `--monitor` and the `train`/`serve` fixes.
   - `openadapt-evals`: (a) `OmniParserGrounder`, (b) multi-monitor `LocalAdapter` plus raw-pixel `ApiAgent`, (c) planner prompt and logging, (d) Windows UTF-8 and `temperature` compatibility.
   - `openadapt-grounding`: (a) SSH tunnel and closed port, (b) CUDA 13 Dockerfile, (c) deploy streaming, caching and pause. Commit `uv.lock` only if you want the newer lock format.
   - `openadapt-ml`: restored functions and the kwarg fix. Keep the `pyproject.toml` dependency edits separate or local.
