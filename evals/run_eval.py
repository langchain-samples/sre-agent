"""Run the offline eval suite against the agent.

    python evals/run_eval.py --dry-run          # show what would run, no model calls
    python evals/run_eval.py --limit 5          # cheap smoke run
    python evals/run_eval.py                    # full dataset

This is the piece that did not exist. There was a dataset and there were
evaluators, but nothing ever executed them together, which is why two of the three
evaluators were reading field names the dataset does not define and nobody found
out. If you change the target's return shape, update the contract documented at
the top of evaluators.py.

KNOWN LIMITATION, read before trusting the scores. The dataset describes fictional
resources ("Pod 'api-server-7d8f9c-xkp2v' in namespace 'production'"). Those do not
exist in whatever cluster the agent is pointed at, so its read tools return
not-found. That means tool_coverage measures tool *selection* honestly, while
response_quality is measuring diagnosis built on failed lookups and will read
lower than the agent deserves. Fixing it properly means stubbing the k8s tools to
return the scenario's implied state; until then treat response_quality as a
floor rather than a grade.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

DATASET_FILE = REPO / "evals" / "sre-agent-k8s-eval.jsonl"
DATASET_NAME = os.getenv("EVAL_DATASET_NAME", "sre-agent-k8s-eval")


def extract_tools_called(result: dict) -> list[str]:
    """Tool names in call order, from the langgraph message trail."""
    names: list[str] = []
    for msg in result.get("messages", []) or []:
        for call in (getattr(msg, "tool_calls", None) or []):
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name:
                names.append(name)
    return names


def final_text(result: dict) -> str:
    messages = result.get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    content = getattr(last, "content", last)
    return content if isinstance(content, str) else str(content)


def build_target():
    """Return a callable matching the output contract in evaluators.py."""
    from agent import create_sre_agent
    from config import make_agent_config

    agent = create_sre_agent()

    def target(inputs: dict) -> dict:
        scenario = inputs.get("scenario", "")
        config = make_agent_config(f"eval-{uuid.uuid4().hex[:8]}")
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": scenario}]},
                                  config=config)
        except Exception as exc:  # a crashed run is a data point, not a lost row
            return {"response": f"ERROR: {type(exc).__name__}: {exc}",
                    "tools_called": [], "health_report": None}
        return {"response": final_text(result),
                "tools_called": extract_tools_called(result),
                "health_report": None}

    return target


def load_local_examples(limit: int | None) -> list[dict]:
    rows = [json.loads(line) for line in DATASET_FILE.read_text().splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="only run the first N examples")
    ap.add_argument("--dry-run", action="store_true", help="no model calls; print the plan")
    ap.add_argument("--dataset", default=DATASET_NAME, help="LangSmith dataset name")
    ap.add_argument("--no-judge", action="store_true",
                    help="skip response_quality (avoids one model call per example)")
    args = ap.parse_args()

    from evaluators import (finding_specificity, deferred_work, response_quality,
                            severity_accuracy, tool_coverage)

    evaluators = [severity_accuracy, tool_coverage, deferred_work, finding_specificity]
    if not args.no_judge:
        evaluators.append(response_quality)

    rows = load_local_examples(args.limit)
    print(f"dataset file : {DATASET_FILE.name} ({len(rows)} example(s))")
    print(f"evaluators   : {[e.__name__ for e in evaluators]}")

    if args.dry_run:
        print("\n--dry-run: no model calls. First 3 scenarios:")
        for r in rows[:3]:
            print(f"  - {r['inputs']['scenario'][:96]}")
        print("\nRemove --dry-run to execute. Note the limitation in this file's docstring:")
        print("scenarios name fictional resources, so read tools will return not-found.")
        return 0

    if not os.getenv("LANGSMITH_API_KEY"):
        print("\nERROR: LANGSMITH_API_KEY is not set. The runner needs it to read the")
        print("dataset and write results. Note that LANGSMITH_RUNS_ENDPOINTS carries")
        print("keys for trace export only; the dataset client does not consult it.")
        return 1

    from langsmith import evaluate

    results = evaluate(
        build_target(),
        data=args.dataset,
        evaluators=evaluators,
        experiment_prefix="sre-agent",
        max_concurrency=2,
    )
    print(f"\nExperiment: {getattr(results, 'experiment_name', '(see LangSmith)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
