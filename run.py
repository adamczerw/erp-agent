import argparse
import logging
import os

from dotenv import load_dotenv
from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.agents.scripted_agent import ScriptedAgent
from openadapt_evals.benchmarks.runner import EvaluationConfig, compute_metrics, evaluate_agent_on_benchmark

from erp_agent.adapter.adapter import DolibarrAdapter
from erp_agent.desktop.vnc_desktop import VncDesktop

parser = argparse.ArgumentParser()
parser.add_argument("address")
parser.add_argument("--task", default="terminal-hello")
args = parser.parse_args()

load_dotenv()
logging.basicConfig(level=logging.INFO)

agent = ScriptedAgent([
    BenchmarkAction("click", x=568, y=776),
    BenchmarkAction("wait"),
    BenchmarkAction("type", text="echo hello world\n"),
])
config = EvaluationConfig(output_dir="data/runs", run_name="smoke", enable_live_tracking=False)

with VncDesktop(args.address, os.environ["VNC_PASSWORD"]) as desktop:
    adapter = DolibarrAdapter(desktop)
    results = evaluate_agent_on_benchmark(agent, adapter, task_ids=[args.task], config=config)

print(compute_metrics(results))
