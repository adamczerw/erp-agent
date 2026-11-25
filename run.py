import argparse
import logging
import os

from dotenv import load_dotenv
from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.agents.scripted_agent import ScriptedAgent
from openadapt_evals.benchmarks.runner import EvaluationConfig, compute_metrics, evaluate_agent_on_benchmark

from erp_agent.adapter.adapter import DolibarrAdapter
from erp_agent.desktop.browser import Browser
from erp_agent.desktop.vnc_desktop import VncDesktop
from erp_agent.dolibarr.dolibarr import Dolibarr

# Hand-written action sequences, one per task. Coordinates are pixels on the 1280x800 screen.
SCRIPTS = {
    "terminal-hello": [
        BenchmarkAction("click", x=568, y=776),
        BenchmarkAction("wait"),
        BenchmarkAction("type", text="echo hello world\n"),
    ],
    "create-thirdparty-name-only": [
        BenchmarkAction("click", x=113, y=80),     # top menu: Third parties
        BenchmarkAction("click", x=70, y=233),     # left menu: New Third Party
        BenchmarkAction("type", text="Nvidia\n"),  # name field has autofocus, Enter submits
    ],
}

parser = argparse.ArgumentParser()
parser.add_argument("address")
parser.add_argument("--task", default="terminal-hello", choices=SCRIPTS)
parser.add_argument("--no-actions", action="store_true", help="send done at once (the task must fail)")
args = parser.parse_args()

load_dotenv()
logging.basicConfig(level=logging.INFO)

agent = ScriptedAgent([] if args.no_actions else SCRIPTS[args.task])
run_name = args.task + ("-no-actions" if args.no_actions else "")
config = EvaluationConfig(output_dir="data/runs", run_name=run_name, enable_live_tracking=False)

with VncDesktop(args.address, os.environ["VNC_PASSWORD"]) as desktop:
    # Tasks with an evaluation_spec are checked in the Dolibarr DB, so they need the backend
    # (DB reset, SQL) and the browser (fresh Chrome on the start page).
    task = DolibarrAdapter(desktop).load_task(args.task)
    backend = browser = None
    if task.evaluation_spec:
        backend = Dolibarr(
            os.environ["BASELINE_DIR"], os.environ["DOCS_DIR"],
            os.environ["DB_ROOT_USER"], os.environ["DB_ROOT_PASSWORD"],
        )
        browser = Browser()
    adapter = DolibarrAdapter(desktop, backend=backend, browser=browser)
    results = evaluate_agent_on_benchmark(agent, adapter, task_ids=[args.task], config=config)

for result in results:
    print(f"{result.task_id}: success={result.success} score={result.score:.2f} reason={result.reason}")
    if result.error:
        print(f"  error ({result.error_type}): {result.error}")
print(compute_metrics(results))
