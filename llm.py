"""Provider-aware LangChain model construction."""
from functools import lru_cache

from config import LLM_PROVIDER, MODEL_ID, SUBAGENT_MODEL_ID


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
