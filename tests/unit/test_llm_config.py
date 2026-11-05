from sqlagent.config import Settings
from sqlagent.llm.client import structured_method


def test_provider_defaults():
    assert Settings(llm_provider="ollama").resolved_llm_model == "qwen2.5-coder:7b"
    assert Settings(llm_provider="anthropic").resolved_llm_model == "claude-opus-5-5"
    assert Settings(llm_provider="anthropic", llm_model="claude-haiku-5-5").resolved_llm_model == "claude-haiku-5-5"


def test_structured_output_method_per_provider():
    assert structured_method(Settings(llm_provider="anthropic")) == "json_schema"
    assert structured_method(Settings(llm_provider="openai")) == "function_calling"
    assert structured_method(Settings(llm_provider="ollama")) is None


def test_anthropic_model_sends_no_temperature(monkeypatch):
    import pytest
    pytest.importorskip("langchain_anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    from sqlagent.llm.client import build_chat_model
    m = build_chat_model(Settings(llm_provider="anthropic"))
    assert m.model == "claude-opus-5-5" and m.temperature is None and m.max_tokens == 16000
