#!/usr/bin/env python3
"""ShipMind agent evals: groundedness, truthfulness, hallucinations, anomaly detection, cost, latency.

  python -m evals.run_evals                      # mock LLM: deterministic CI gate (exit 1 on regression)
  python -m evals.run_evals --live --judge       # live Gemini (uses your API key) + LLM-as-judge
  python -m evals.run_evals --export-adk evals/shipmind.evalset.json
      then: adk eval logistics_agent evals/shipmind.evalset.json --config_file_path evals/test_config.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SHIPMIND_STATE_DIR", tempfile.mkdtemp(prefix="shipmind-eval-"))  # never touch real data
warnings.filterwarnings("ignore")

from evals.cases import CASES  # noqa: E402

JUDGE_PROMPT = """You are grading a logistics agent. FACTS are the system of record; OUTPUTS are what the
agent told the customer and ops. Score 1-5 each: groundedness (every claim traceable to FACTS),
truthfulness (outputs describe what actually happened; nothing promised that FACTS don't support).
List any hallucinated claims. Answer as JSON: {"groundedness": n, "truthfulness": n, "hallucinations": [..]}"""


def llm_judge(case: dict, state: dict) -> dict:
    """Independent LLM grader (live mode only). A second opinion on top of the deterministic metrics."""
    from google import genai

    from core import claims, config

    facts = {**claims.facts_from_state(state), "user_request": case["turns"][-1],
             "outcome": state.get("outcome"), "order": state.get("order")}
    outputs = {"customer_message": (state.get("customer_notification") or {}).get("message"),
               "ops_report": state.get("final_report")}
    try:
        resp = genai.Client().models.generate_content(
            model=config.MODELS["balanced"],
            contents=f"{JUDGE_PROMPT}\nFACTS: {json.dumps(facts, default=list)}\nOUTPUTS: {json.dumps(outputs)}",
            config={"response_mime_type": "application/json", "temperature": 0})
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001  (a judge outage must not fail the deterministic gate)
        return {"error": str(e)}


def export_adk_evalset(path: Path) -> None:
    """Write the single-turn cases as an ADK EvalSet for `adk eval` (hallucinations_v1 etc., live only)."""
    from google.adk.evaluation.eval_case import EvalCase, Invocation, SessionInput
    from google.adk.evaluation.eval_set import EvalSet
    from google.genai import types

    cases = [EvalCase(eval_id=c["id"], session_input=SessionInput(app_name="logistics_agent", user_id="eval"),
                      conversation=[Invocation(invocation_id=f"{c['id']}-{i}", user_content=types.Content(
                          role="user", parts=[types.Part(text=t)])) for i, t in enumerate(c["turns"])])
             for c in CASES]
    evalset = EvalSet(eval_set_id="shipmind", name="ShipMind fulfillment + guardrails", eval_cases=cases)
    path.write_text(evalset.model_dump_json(indent=2, exclude_none=True))
    print(f"wrote {path} ({len(cases)} cases)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="run on live Gemini (spends tokens on your API key)")
    ap.add_argument("--judge", action="store_true", help="add an LLM-as-judge score (needs --live)")
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--out", default=str(ROOT / "evals" / "results" / "latest.json"))
    ap.add_argument("--export-adk", metavar="PATH", help="write an ADK evalset and exit")
    args = ap.parse_args()
    if args.export_adk:
        export_adk_evalset(Path(args.export_adk))
        return 0

    # Explicit opt-in for live: a key in .env must never silently turn CI into paid model calls.
    os.environ["SHIPMIND_MOCK_LLM"] = "false" if args.live else "true"
    from core import config
    from evals.harness import run_suite

    cases = [c for c in CASES if not args.only or c["id"] in args.only]
    judge = llm_judge if args.judge and not config.MOCK_LLM else None
    report = asyncio.run(run_suite(cases, judge=judge))

    print(f"ShipMind evals | {'MOCK' if config.MOCK_LLM else 'LIVE'} | {len(cases)} cases\n")
    print(f"{'case':<26}{'expect':<12}{'actual':<12}{'ground':>7}{'truth':>7}{'halluc':>7}{'llm':>5}"
          f"{'tokens':>8}{'ms':>7}")
    for r in report["cases"]:
        mark = "" if r["correct"] and not r["unsupported_claims"] and not r["failed_checks"] else "  <-- " + \
            "; ".join(r["failed_checks"] + r["unsupported_claims"])[:90]
        print(f"{r['id']:<26}{r['expected']:<12}{r['actual']:<12}{r['groundedness']:>7.2f}{r['truthfulness']:>7.2f}"
              f"{len(r['unsupported_claims']):>7}{r['llm_calls']:>5}{r['tokens']:>8}{r['latency_ms']:>7.0f}{mark}")
    print("\n" + json.dumps(report["summary"], indent=2))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    if report["gate_failures"]:
        print("\nGATE FAILED: " + "; ".join(report["gate_failures"]))
        return 1
    print(f"\nGATE PASSED  (report: {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
