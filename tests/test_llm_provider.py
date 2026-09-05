"""Provider selection and model-construction tests."""
import importlib
import sys
import types

import pytest


def _reload_config(monkeypatch, provider: str):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_anthropic_remains_the_default_provider(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    sys.modules.pop("config", None)
    config = importlib.import_module("config")

    assert config.LLM_PROVIDER == "anthropic"
    assert config.MODEL == "anthropic:claude-sonnet-4-6"
    assert config.SUBAGENT_MODEL == "anthropic:claude-haiku-4-5-20251001"


def test_openai_uses_tiered_default_models(monkeypatch):
    config = _reload_config(monkeypatch, "openai")

    assert config.MODEL == "openai:gpt-5.6-sol"
    assert config.SUBAGENT_MODEL == "openai:gpt-5.6-luna"


def test_invalid_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    sys.modules.pop("config", None)

    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        importlib.import_module("config")


def test_openai_model_uses_responses_api(monkeypatch):
    config = _reload_config(monkeypatch, "openai")
    calls = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI),
    )
    sys.modules.pop("llm", None)
    llm = importlib.import_module("llm")

    assert isinstance(llm.get_main_model(), FakeChatOpenAI)
    assert calls == [{"model": config.MODEL_ID, "use_responses_api": True}]
