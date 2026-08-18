"""Upload online evaluators to LangSmith, to score production traces.

Online evaluators run against live traces, which have **no ground-truth example**.
That makes reference-based metrics structurally unable to work here: comparing the
agent's answer to a reference is meaningless when there is no reference.

The previous version of this file shipped hand-copied reference-based evaluators.
They read `expected_response` out of the *run's* outputs, a field only a dataset
example carries, so on every production trace they compared "" against "" and
recorded severity_accuracy=0 and tool_coverage=1.0 regardless of what the agent
did. Constant scores are worse than no scores, because they look like data.

Two changes here. Only reference-free evaluators are uploaded, and their source is
read from evaluators.py with inspect.getsource instead of being maintained as a
second copy that drifts. That is also why those functions keep their imports and
helpers inline: LangSmith executes them in a sandbox with no access to this repo.

    LANGSMITH_API_KEY=... LANGSMITH_PROJECT_ID=... python evals/upload_online_evals.py
    python evals/upload_online_evals.py --print   # show what would be uploaded
"""
from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.evaluators import REFERENCE_FREE  # noqa: E402

BASE_URL = "https://api.smith.langchain.com"


def to_sandbox_source(fn) -> str:
    """Render an evaluator as standalone source for the LangSmith sandbox."""
    src = inspect.getsource(fn)
    # The sandbox calls perform_eval(run, example).
    src = re.sub(rf"\bdef\s+{re.escape(fn.__name__)}\s*\(", "def perform_eval(", src, count=1)
    # Reference-free evaluators take example=None; the sandbox always passes two args.
    return src.replace("def perform_eval(run, example=None):", "def perform_eval(run, example):")


def upload(name: str, code: str) -> bool:
    import requests

    payload = {
        "display_name": name,
        "sampling_rate": 1.0,
        "session_id": os.environ["LANGSMITH_PROJECT_ID"],
        "code_evaluators": [{"code": code, "language": "python"}],
    }
    r = requests.post(
        f"{BASE_URL}/runs/rules",
        json=payload,
        headers={"x-api-key": os.environ["LANGSMITH_API_KEY"],
                 "Content-Type": "application/json"},
    )
    ok = r.status_code == 200
    print(f"[{r.status_code}] {name}: {'OK' if ok else r.text[:300]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", action="store_true", dest="show",
                    help="print the generated source instead of uploading")
    args = ap.parse_args()

    print(f"reference-free evaluators: {[f.__name__ for f in REFERENCE_FREE]}")
    print("(reference-based evaluators are offline-only; see this file's docstring)\n")

    for fn in REFERENCE_FREE:
        code = to_sandbox_source(fn)
        title = fn.__name__.replace("_", " ").title()
        if args.show:
            print(f"{'=' * 70}\n{title}\n{'=' * 70}\n{code}")
            continue
        for var in ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT_ID"):
            if not os.getenv(var):
                print(f"ERROR: {var} is not set.")
                if var == "LANGSMITH_API_KEY":
                    print("  Note: LANGSMITH_RUNS_ENDPOINTS carries keys for trace export")
                    print("  only. This endpoint needs LANGSMITH_API_KEY directly.")
                return 1
        upload(title, code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
