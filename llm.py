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


def request_health_report(schema: dict, system: str, user: str):
    """Return an unvalidated report payload for shared validation and repair.

    Pass a JSON schema rather than a Pydantic class to OpenAI so enum drift
    reaches our repair step instead of raising inside the SDK's model parser.
    """
    if LLM_PROVIDER == "openai":
        model = get_subagent_model().with_structured_output(
            schema, method="json_schema",
        )
        return model.invoke([("system", system), ("user", user)])

    import anthropic
    from langsmith.wrappers import wrap_anthropic

    client = wrap_anthropic(anthropic.Anthropic(api_key=os.getenv(PROVIDER_API_KEY, "")))
    response = client.messages.create(
        model=SUBAGENT_MODEL_ID,
        max_tokens=4096,
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
    if payload is None:
        stop_reason = getattr(response, "stop_reason", None)
        log.error("Anthropic returned no health report (stop_reason=%s)", stop_reason)
        if stop_reason == "max_tokens":
            raise HealthReportTokenLimitError("Health analysis hit the output token limit")
    return payload
