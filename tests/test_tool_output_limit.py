"""Tests for the per-tool-result size cap.

Context reached 632,740 tokens against a 200,000 ceiling while every existing
limit was satisfied, because every one of them counts calls rather than bytes:
TOOL_CALL_RUN_LIMIT of 80 times roughly 8k tokens per result is 640k. These tests
cover the cap that bounds per-step growth, and specifically that the model is told
content was elided rather than being silently handed a truncated result.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent import _fanout_output_used, truncate_tool_output
from config import TASK_FANOUT_OUTPUT_MAX_CHARS, TOOL_OUTPUT_MAX_CHARS


def call_with(content, tool_name="kubectl_get_pod_logs"):
    """Drive the middleware with a stubbed handler returning `content`."""
    request = SimpleNamespace(tool_name=tool_name,
                             tool=SimpleNamespace(name=tool_name))
    handler = lambda req: ToolMessage(content=content, tool_call_id="tc-1")
    # wrap_tool_call returns an AgentMiddleware; the hook is wrap_tool_call.
    return truncate_tool_output.wrap_tool_call(request, handler)


def call_task_with(content, state):
    request = SimpleNamespace(
        tool_name="task",
        tool=SimpleNamespace(name="task"),
        state=state,
    )
    result = Command(
        update={"messages": [ToolMessage(content=content, tool_call_id="tc-1")]}
    )
    handler = lambda req: result
    return truncate_tool_output.wrap_tool_call(request, handler)


def test_small_output_passes_through_untouched():
    out = call_with("all good")
    assert out.content == "all good"


def test_output_exactly_at_the_limit_is_not_truncated():
    payload = "x" * TOOL_OUTPUT_MAX_CHARS
    assert call_with(payload).content == payload


def test_oversized_output_is_truncated():
    payload = "y" * (TOOL_OUTPUT_MAX_CHARS + 50_000)
    out = call_with(payload).content
    assert len(out) < len(payload)
    assert out.startswith("y" * 100)


def test_truncation_tells_the_model_what_happened():
    """Silently dropping the tail is worse than the overflow it prevents."""
    payload = "z" * (TOOL_OUTPUT_MAX_CHARS + 12_345)
    out = call_with(payload).content
    assert "[TRUNCATED:" in out
    assert "kubectl_get_pod_logs" in out
    assert f"{len(payload):,}" in out          # original size stated
    assert f"{12_345:,}" in out                # dropped amount stated
    assert "Narrow the request" in out         # actionable next step


# deepagents triggers summarization at 170k against a 200k ceiling, so a single
# step has ~30k tokens of headroom. That is the number the cap has to respect:
# summarization collapses history *between* steps but cannot stop one step from
# overshooting, which is exactly how the 632k prompt happened.
SUMMARIZATION_HEADROOM_TOKENS = 30_000


def test_one_parallel_step_fits_inside_the_summarization_headroom():
    per_call_tokens = TOOL_OUTPUT_MAX_CHARS // 4
    for parallel_calls in (4, 8):
        step = parallel_calls * per_call_tokens
        assert step < SUMMARIZATION_HEADROOM_TOKENS, (
            f"{parallel_calls} capped results is ~{step:,} tokens, which exceeds "
            f"the ~{SUMMARIZATION_HEADROOM_TOKENS:,} available before the ceiling")


def test_the_largest_output_we_actually_saw_would_be_truncated():
    """20,451 chars from kubectl_get_pod_logs, the biggest in the failing trace."""
    out = call_with("L" * 20_451).content
    assert "[TRUNCATED:" in out
    assert len(out) < 20_451 + 400


def test_task_command_result_is_truncated():
    state = {"messages": [SimpleNamespace(tool_calls=[{"id": "step-1"}])]}
    out = call_task_with("t" * (TOOL_OUTPUT_MAX_CHARS + 100), state)
    content = out.update["messages"][0].content
    assert "[TRUNCATED:" in content
    assert len(content) <= TOOL_OUTPUT_MAX_CHARS


def test_parallel_task_results_share_an_aggregate_budget():
    state = {"messages": [SimpleNamespace(tool_calls=[{"id": "step-2"}])]}
    outputs = [
        call_task_with("u" * TOOL_OUTPUT_MAX_CHARS, state)
        for _ in range(8)
    ]
    total = sum(len(output.update["messages"][0].content) for output in outputs)
    assert total <= TASK_FANOUT_OUTPUT_MAX_CHARS + 200
    assert "[TRUNCATED:" in outputs[-1].update["messages"][0].content
    _fanout_output_used.clear()


def test_non_string_content_is_left_alone():
    request = SimpleNamespace(tool_name="t", tool=SimpleNamespace(name="t"))
    blocks = [{"type": "text", "text": "structured"}]
    handler = lambda req: ToolMessage(content=blocks, tool_call_id="tc-2")
    assert truncate_tool_output.wrap_tool_call(request, handler).content == blocks


def test_missing_tool_name_does_not_raise():
    request = SimpleNamespace()
    payload = "q" * (TOOL_OUTPUT_MAX_CHARS + 10)
    handler = lambda req: ToolMessage(content=payload, tool_call_id="tc-3")
    out = truncate_tool_output.wrap_tool_call(request, handler).content
    assert "[TRUNCATED:" in out


def test_middleware_is_registered_first():
    """It must wrap every tool call, so it goes at the head of the stack."""
    import agent
    assert type(agent._build_middleware()[0]).__name__ == "truncate_tool_output"
