"""Wiring: build the production Agent (LLM, checkpointer, audit, optional Langfuse tracing)."""
from __future__ import annotations

import logging
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from sqlagent.agent.graph import Services
from sqlagent.agent.service import Agent
from sqlagent.config import Settings, get_settings
from sqlagent.llm.client import LangChainLLM, StructuredLLM, build_chat_model, structured_method
from sqlagent.observability.audit import AuditLogger

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


def ensure_warehouse(settings: Settings | None = None) -> str:
    """First-start bootstrap for hosted demos: if there is no warehouse, fetch the Kaggle files
    from the verified GitHub mirrors (or, without network, generate same-schema sample data) and
    build it. Returns the data source used ("existing", "kaggle-mirror" or "sample")."""
    s = settings or get_settings()
    if s.warehouse_path.exists():
        return "existing"
    from warehouse import build, download, sample
    raw = s.warehouse_path.parent / "raw"
    source = "kaggle-mirror"
    if not (raw / "olist" / "olist_orders_dataset.csv").exists():
        try:
            download.download_mirrors(raw)
        except Exception as e:  # noqa: BLE001 - offline host: fall back to sample data
            log.warning("mirror download failed (%s); generating sample data", e)
            sample.generate(raw)
            source = "sample"
    s.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    build.build(raw, s.warehouse_path)
    return source


def build_agent(settings: Settings | None = None, llm: StructuredLLM | None = None) -> Agent:
    s = settings or get_settings()
    if not s.warehouse_path.exists():
        raise FileNotFoundError(f"warehouse not found at {s.warehouse_path}; run `halcyon-warehouse build` first")
    llm = llm or LangChainLLM(build_chat_model(s), callbacks=_callbacks(s), structured_method=structured_method(s))
    services = Services.from_settings(s, llm)
    s.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(s.checkpoint_path), check_same_thread=False))
    return Agent(services, saver, AuditLogger(s.audit_path))
