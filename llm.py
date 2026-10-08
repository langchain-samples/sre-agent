"""Provider-aware LangChain model construction."""
from functools import lru_cache
import logging
import os

from config import LLM_PROVIDER, MODEL_ID, PROVIDER_API_KEY, SUBAGENT_MODEL_ID

log = logging.getLogger("sre-agent.llm")


class HealthReportTokenLimitError(RuntimeError):
    """The provider exhausted its output budget before producing a report."""


def validate_provider_credentials() -> None:
    """Fail with a clear configuration error before constructing model clients."""
    key_name = PROVIDER_API_KEY
    if not os.getenv(key_name, "").strip():
        raise RuntimeError(
            f"{key_name} is required when LLM_PROVIDER={LLM_PROVIDER}; "
            "set it before starting SRE Agent."
        )


@lru_cache(maxsize=2)
def _build_model(model_id: str):
    """Return a configured model for the selected provider.

    Anthropic stays as a provider-qualified model string so Deep Agents keeps
    its existing initialization path. OpenAI is instantiated explicitly to use
    the Responses API, which supports reasoning and function tools together.
    """
    if LLM_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_id, use_responses_api=True)
    return f"anthropic:{model_id}"


def get_main_model():
    return _build_model(MODEL_ID)


def get_subagent_model():
    return _build_model(SUBAGENT_MODEL_ID)


def _health_report_max_tokens() -> int:
    """Return a positive Anthropic output budget, falling back on invalid configuration."""
    try:
        max_tokens = int(os.getenv("ANTHROPIC_HEALTH_REPORT_MAX_TOKENS", "8192"))
        if max_tokens > 0:
            return max_tokens
    except ValueError:
        pass
    log.warning("Invalid ANTHROPIC_HEALTH_REPORT_MAX_TOKENS; using 8192")
    return 8192


def request_health_report(schema: dict, system: str, user: str):
    """Return an unvalidated report payload for shared validation and repair."""
    if LLM_PROVIDER == "openai":
        model = get_subagent_model().with_structured_output(
            schema, method="json_schema", include_raw=True,
        )
        result = model.invoke([("system", system), ("user", user)])
        metadata = result["raw"].response_metadata
        if (
            metadata.get("finish_reason") == "length"
            or (metadata.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
        ):
            raise HealthReportTokenLimitError("Health analysis hit the output token limit")
        if result.get("parsing_error") is not None:
            raise result["parsing_error"]
        return result["parsed"]

    import anthropic
    from langsmith.wrappers import wrap_anthropic

    client = wrap_anthropic(anthropic.Anthropic(api_key=os.getenv(PROVIDER_API_KEY, "")))
    response = client.messages.create(
        model=SUBAGENT_MODEL_ID,
        max_tokens=_health_report_max_tokens(),
        system=system,
        tools=[{
            "name": "report_health",
            "description": "Report the structured cluster health assessment.",
            "input_schema": schema,
        }],
        tool_choice={"type": "tool", "name": "report_health"},
        messages=[{"role": "user", "content": user}],
    )
    payload = next(
        (block.input for block in response.content if getattr(block, "type", None) == "tool_use"),
        None,
    )
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        findings = payload.get("findings") if isinstance(payload, dict) else None
        log.error(
            "Anthropic health report hit the output token limit (partial_findings=%d)",
            len(findings) if isinstance(findings, list) else 0,
        )
        raise HealthReportTokenLimitError("Health analysis hit the output token limit")
    if payload is None:
        log.error("Anthropic returned no health report (stop_reason=%s)", stop_reason)
    return payload
