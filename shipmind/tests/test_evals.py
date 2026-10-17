import asyncio
import json
from pathlib import Path

from google.adk.evaluation.eval_config import EvalConfig

from evals.cases import CASES
from evals.harness import run_suite

ROOT = Path(__file__).resolve().parent.parent


def test_eval_suite_passes_quality_gate():
    report = asyncio.run(run_suite(CASES))
    assert report["gate_failures"] == [], report["summary"]
    s = report["summary"]
    assert s["hallucination_rate"] == 0 and s["anomaly_recall"] == 1.0 and s["groundedness"] >= 0.95


def test_adk_eval_config_is_valid():
    EvalConfig.model_validate(json.loads((ROOT / "evals" / "test_config.json").read_text()))
