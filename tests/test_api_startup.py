"""The API should reject missing model credentials before starting dependencies."""

import asyncio

import pytest

import api
import llm


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_provider_key_fails_before_api_startup(monkeypatch, provider, value):
    key = f"{provider.upper()}_API_KEY"
    monkeypatch.setattr(llm, "LLM_PROVIDER", provider)
    monkeypatch.setattr(llm, "PROVIDER_API_KEY", key)
    if value is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, value)

    async def start():
        async with api.lifespan(api.app):
            pass

    with pytest.raises(RuntimeError, match=f"{key} is required"):
        asyncio.run(start())


def test_renderer_asset_is_served_without_starting_agent(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    response = TestClient(api.app).get("/assets/markdown-it.min.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "markdown-it" in response.text
