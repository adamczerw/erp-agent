## Architecture

```
openadapt_ml/
├── schema/              # Episode, Step, Action, Observation (Pydantic models)
│   ├── episode.py       #   Core dataclasses + JSON Schema export
│   └── converters.py    #   WAA/WebArena format converters
├── models/              # VLM adapters
│   ├── base_adapter.py  #   BaseVLMAdapter ABC
│   ├── qwen_vl.py       #   Qwen3-VL, Qwen2.5-VL
│   ├── api_adapter.py   #   Claude, GPT (inference-only)
│   └── dummy_adapter.py #   Fake adapter for testing
├── training/            # Fine-tuning pipeline
│   ├── trl_trainer.py   #   TRL SFTTrainer + Unsloth
│   ├── trainer.py       #   Training orchestration
│   └── viewer.py        #   Training dashboard (HTML)
├── runtime/             # Inference
│   ├── policy.py        #   AgentPolicy (screenshot -> action)
│   └── safety_gate.py   #   Action safety checks
├── datasets/            # Data loading
│   └── next_action.py   #   Episodes -> SFT chat samples
├── ingest/              # Data ingestion
│   ├── synthetic.py     #   Synthetic UI generation
│   ├── capture.py       #   openadapt-capture loader
│   └── loader.py        #   Generic episode loader
├── grounding/           # UI element localization
│   ├── base.py          #   OracleGrounder, GroundingModule ABC
│   └── detector.py      #   GeminiGrounder, SoM overlays
├── retrieval/           # Demo-conditioned inference
│   ├── retriever.py     #   Demo retrieval for RAG prompting
│   └── embeddings.py    #   Screenshot/action embeddings
├── benchmarks/          # ML-specific benchmark agents
│   └── agent.py         #   PolicyAgent, APIBenchmarkAgent, UnifiedBaselineAgent
├── cloud/               # Cloud GPU training
│   ├── lambda_labs.py   #   Lambda Labs integration
│   ├── local.py         #   Local training (CUDA/MPS)
│   └── ssh_tunnel.py    #   SSH tunnel management
├── segmentation/        # Recording segmentation pipeline
├── evals/               # Evaluation metrics (grounding, trajectory matching)
├── config.py            # Settings via pydantic-settings
└── scripts/             # CLI entry points (train, eval, compare, demo)
```