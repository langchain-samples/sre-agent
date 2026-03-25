"""Upload the three SRE-agent online evaluators to LangSmith."""
import os
import re
import requests

API_KEY = os.environ["LANGSMITH_API_KEY"]
PROJECT_ID = os.environ["LANGSMITH_PROJECT_ID"]
BASE_URL = "https://api.smith.langchain.com"
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


def upload(name: str, code: str) -> None:
    func_name = re.search(r"def (\w+)\(", code).group(1)
    code = re.sub(
        rf"\bdef\s+{re.escape(func_name)}\s*\(",
        "def perform_eval(",
        code,
        count=1,
    )
    payload = {
        "display_name": name,
        "sampling_rate": 1.0,
        "session_id": PROJECT_ID,
        "code_evaluators": [{"code": code, "language": "python"}],
    }
    r = requests.post(f"{BASE_URL}/runs/rules", json=payload, headers=HEADERS)
    status = "OK" if r.status_code == 200 else r.text[:300]
    print(f"[{r.status_code}] {name}: {status}")


# ── 1. Severity Accuracy ──────────────────────────────────────────────────────

SEVERITY_ACCURACY = """
import re

def severity_accuracy(run, example):
    pattern = re.compile(r'\\[(CRITICAL|WARNING|INFO|OK)\\]', re.IGNORECASE)
    def extract(text):
        m = pattern.search(text)
        return m.group(1).upper() if m else None
    actual   = extract((run.get('outputs')     or {}).get('expected_response', ''))
    expected = extract((example.get('outputs') or {}).get('expected_response', ''))
    if actual is None or expected is None:
        return {'key': 'severity_accuracy', 'score': 0,
                'comment': f'Missing severity bracket — actual={actual}, expected={expected}'}
    return {'key': 'severity_accuracy',
            'score': 1 if actual == expected else 0,
            'comment': f'actual={actual}, expected={expected}'}
"""

# ── 2. Tool Coverage ──────────────────────────────────────────────────────────

TOOL_COVERAGE = """
def tool_coverage(run, example):
    actual_traj   = (run.get('outputs')     or {}).get('expected_trajectory', [])
    expected_traj = (example.get('outputs') or {}).get('expected_trajectory', [])
    if not expected_traj:
        return {'key': 'tool_coverage', 'score': 1.0,
                'comment': 'No expected trajectory to check against'}
    actual_set   = set(actual_traj)
    expected_set = set(expected_traj)
    covered = actual_set & expected_set
    score   = len(covered) / len(expected_set)
    missing = sorted(expected_set - actual_set)
    extra   = sorted(actual_set   - expected_set)
    parts   = [f'covered {len(covered)}/{len(expected_set)} tools']
    if missing:
        parts.append(f'missing={missing}')
    if extra:
        parts.append(f'extra={extra}')
    return {'key': 'tool_coverage', 'score': round(score, 3), 'comment': ', '.join(parts)}
"""

# ── 3. Response Quality (LLM-as-judge via Anthropic Haiku) ───────────────────

RESPONSE_QUALITY = """
import os
import json
import re
import anthropic

def response_quality(run, example):
    agent_text    = (run.get('outputs')     or {}).get('expected_response', '')
    expected_text = (example.get('outputs') or {}).get('expected_response', '')

    prompt = (
        'You are evaluating an SRE agent Kubernetes response.\\n\\n'
        'Expected (ground truth):\\n' + expected_text + '\\n\\n'
        'Agent response:\\n' + agent_text + '\\n\\n'
        'Score 1-5:\\n'
        '5=correct diagnosis + specific resources + clear remediation\\n'
        '4=correct + mostly specific + vague remediation\\n'
        '3=partially correct, some specifics missing\\n'
        '2=off diagnosis or too generic\\n'
        '1=wrong or irrelevant\\n\\n'
        'Reply with JSON only: {"score": <int>, "specific": <bool>, '
        '"actionable": <bool>, "correct_diagnosis": <bool>, "reasoning": "<str>"}'
    )

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=300,
        messages=[{'role': 'user', 'content': prompt}],
    )
    text = msg.content[0].text
    m = re.search(r'\\{.*\\}', text, re.DOTALL)
    if not m:
        return {'key': 'response_quality', 'score': 0,
                'comment': f'Parse error: {text[:100]}'}
    grade = json.loads(m.group())
    normalized = round((grade['score'] - 1) / 4, 3)
    return {
        'key': 'response_quality',
        'score': normalized,
        'comment': (
            f"score={grade['score']}/5 | specific={grade['specific']}, "
            f"actionable={grade['actionable']}, "
            f"correct_diagnosis={grade['correct_diagnosis']} | "
            f"{grade['reasoning']}"
        ),
    }
"""

if __name__ == "__main__":
    upload("Severity Accuracy", SEVERITY_ACCURACY)
    upload("Tool Coverage", TOOL_COVERAGE)
    upload("Response Quality", RESPONSE_QUALITY)
