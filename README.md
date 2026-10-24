# erp-agent

A GUI agent that automates business processes in the [Dolibarr](https://www.dolibarr.org/) ERP
by looking at the screen and using the mouse and keyboard. Part of a master's thesis.

The agent runs on the host. Dolibarr's GUI runs on a virtual desktop inside a Docker container,
which the agent controls over VNC. Task success is checked against the Dolibarr database.

Built on [OpenAdapt](https://github.com/OpenAdaptAI) (forks under
[adamczerw](https://github.com/adamczerw)). This package depends on `openadapt-evals` and
plugs into its `BenchmarkAgent` and `BenchmarkAdapter` interfaces; the forks don't depend on it.

## Layout

```
src/erp_agent/
  agent/       planner, OmniParserGrounder, ErpAgent orchestrator
  desktop/     VncDesktop: screenshots and input over VNC (RFB)
  dolibarr/    Dolibarr backend (reset, SQL checks) and DolibarrAdapter
tasks/         task definitions (instruction, start URL, success check)
docs/          plan and notes
tests/
experiments/   notebooks and scripts (local only, not in git)
data/          recordings and run outputs (local only, not in git)
```

## Setup

Expects the OpenAdapt forks next to this repo in `../openadapt-all/` and a shared venv in `../.venv`:

```bash
cd ..   # the folder containing erp-agent/ and openadapt-all/
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install --no-sources \
  -e ./openadapt-all/OpenAdapt -e ./openadapt-all/openadapt-capture \
  -e ./openadapt-all/openadapt-evals -e ./openadapt-all/openadapt-grounding \
  -e ./openadapt-all/openadapt-ml
uv pip install --no-deps -e "./erp-agent[dev]"
pytest erp-agent/tests
```

API keys go in `.env` (not committed).
