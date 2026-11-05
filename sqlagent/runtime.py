"""Wiring: build the production Agent (LLM, checkpointer, audit, optional Langfuse tracing)."""
from __future__ import annotations

import logging
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from sqlagent.agent import Agent
from sqlagent.audit import AuditLogger
from sqlagent.config import Settings, get_settings
from sqlagent.graph import Services
from sqlagent.llm import LangChainLLM, StructuredLLM, build_chat_model

log = logging.getLogger(__name__)


def _callbacks(s: Settings) -> list:
    if not s.langfuse_enabled:
        return []
    try:  # pip install .[observability]; configure LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST
        from langfuse.langchain import CallbackHandler
    except ImportError:
        try:
            from langfuse.callback import CallbackHandler  # langfuse v2
        except ImportError:
            log.warning("langfuse_enabled but langfuse is not installed")
            return []
    return [CallbackHandler()]


def build_agent(settings: Settings | None = None, llm: StructuredLLM | None = None) -> Agent:
    s = settings or get_settings()
    if not s.warehouse_path.exists():
        raise FileNotFoundError(f"warehouse not found at {s.warehouse_path}; run setup/build_warehouse.py first")
    llm = llm or LangChainLLM(build_chat_model(s), callbacks=_callbacks(s))
    services = Services.from_settings(s, llm)
    s.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(s.checkpoint_path), check_same_thread=False))
    return Agent(services, saver, AuditLogger(s.audit_path))
