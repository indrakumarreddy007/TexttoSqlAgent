"""LLM access behind one small interface.

    llm.generate(Schema, system, user) -> Schema instance

`LangChainLLM` wraps any LangChain chat model with `with_structured_output` (Ollama by
default; Anthropic or any OpenAI-compatible endpoint such as Groq by config). `ScriptedLLM`
replays canned responses so the graph is tested deterministically in CI.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from sqlagent.config import Settings

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


class StructuredLLM(Protocol):
    def generate(self, schema: type[T], system: str, user: str) -> T: ...


class LLMError(RuntimeError):
    pass


def build_chat_model(s: Settings) -> Any:
    provider = s.llm_provider.lower()
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        kw = {"base_url": s.llm_base_url} if s.llm_base_url else {}
        return ChatOllama(model=s.llm_model, temperature=s.llm_temperature, num_ctx=8192, **kw)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # pip install .[anthropic]
        return ChatAnthropic(model=s.llm_model, temperature=s.llm_temperature, timeout=s.llm_timeout_s,
                             max_tokens=2048)
    if provider == "openai":
        from langchain_openai import ChatOpenAI  # any OpenAI-compatible server (Groq, vLLM, LM Studio)
        kw = {"base_url": s.llm_base_url} if s.llm_base_url else {}
        return ChatOpenAI(model=s.llm_model, temperature=s.llm_temperature, timeout=s.llm_timeout_s, **kw)
    raise ValueError(f"unknown llm_provider {s.llm_provider!r}")


class LangChainLLM:
    def __init__(self, chat_model: Any, callbacks: list | None = None, retries: int = 1):
        self.chat = chat_model
        self.callbacks = callbacks or []
        self.retries = retries
        self.usage: list[dict] = []

    def generate(self, schema: type[T], system: str, user: str) -> T:
        runnable = self.chat.with_structured_output(schema, include_raw=True)
        messages = [("system", system), ("human", user)]
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                out = runnable.invoke(messages, config={"callbacks": self.callbacks})
            except Exception as e:  # noqa: BLE001 - provider/network errors vary by backend
                last_err = e
                log.warning("LLM call failed (attempt %s): %s", attempt + 1, e)
                continue
            raw = out.get("raw")
            meta = getattr(raw, "usage_metadata", None) or {}
            self.usage.append({"schema": schema.__name__, "ms": int((time.perf_counter() - t0) * 1000), **meta})
            parsed = out.get("parsed")
            if isinstance(parsed, schema):
                return parsed
            if isinstance(parsed, dict):
                try:
                    return schema.model_validate(parsed)
                except ValidationError as e:
                    last_err = e
            else:
                last_err = out.get("parsing_error") or LLMError("no structured output")
            fix = f"Your previous output was invalid: {last_err}. Return only valid {schema.__name__} JSON."
            messages = [*messages, ("human", fix)]
        raise LLMError(f"{schema.__name__} generation failed: {last_err}")


class ScriptedLLM:
    """Deterministic test double. Responses are queued per schema name; a callable may be
    queued instead of a model instance to compute the response from the prompt."""

    def __init__(self, script: dict[str, list[BaseModel | dict | Callable[[str, str], BaseModel]]] | None = None):
        self.queues: dict[str, deque] = defaultdict(deque)
        self.calls: list[dict] = []
        for name, items in (script or {}).items():
            self.queues[name].extend(items)

    def push(self, schema_name: str, item: Any) -> None:
        self.queues[schema_name].append(item)

    def generate(self, schema: type[T], system: str, user: str) -> T:
        self.calls.append({"schema": schema.__name__, "system": system, "user": user})
        q = self.queues[schema.__name__]
        if not q:
            raise LLMError(f"ScriptedLLM has no response queued for {schema.__name__}")
        item = q.popleft()
        if callable(item) and not isinstance(item, BaseModel):
            item = item(system, user)
        return item if isinstance(item, schema) else schema.model_validate(item)
