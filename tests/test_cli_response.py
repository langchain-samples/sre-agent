"""The CLI must render structured Responses API content as text."""

from io import StringIO

from rich.console import Console

import main
import llm
import pytest


class Message:
    def __init__(self, content):
        self.content = content


def test_print_response_accepts_openai_content_blocks(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(main, "console", Console(file=output, width=100))

    main.print_response({"messages": [Message([{"type": "text", "text": "Cluster is healthy."}])]})

    assert "Cluster is healthy." in output.getvalue()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_cli_rejects_missing_or_blank_provider_key(monkeypatch, provider, value):
    key = f"{provider.upper()}_API_KEY"
    monkeypatch.setattr(llm, "LLM_PROVIDER", provider)
    monkeypatch.setattr(llm, "PROVIDER_API_KEY", key)
    if value is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, value)
    output = StringIO()
    monkeypatch.setattr(main, "console", Console(file=output, width=150))
    with pytest.raises(SystemExit) as error:
        main.check_env()
    assert error.value.code == 1
    assert f"{key} is required" in output.getvalue()
