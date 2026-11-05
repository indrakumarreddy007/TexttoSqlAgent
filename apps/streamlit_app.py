"""Halcyon SQL Copilot: simple web UI. Ask in plain English, see the answer.

Run locally:            streamlit run apps/streamlit_app.py
Streamlit Cloud / HF:   main file = apps/streamlit_app.py (see docs/DEPLOY.md)

The agent runs in this process: no separate API server. On first start the app builds the
warehouse itself if it is missing. LLM settings come from environment variables or Streamlit
secrets (SQLAGENT_LLM_PROVIDER, SQLAGENT_LLM_MODEL, SQLAGENT_LLM_BASE_URL, and the provider's
API key).
"""
from __future__ import annotations

import os
import uuid

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Halcyon SQL Copilot", layout="wide")

# Streamlit Cloud / Hugging Face secrets -> environment, before settings are read
try:
    for key, value in st.secrets.items():
        if isinstance(value, str):
            os.environ.setdefault(key, value)
except Exception:  # noqa: BLE001 - no secrets file is fine locally
    pass

from sqlagent.bootstrap import build_agent, ensure_warehouse  # noqa: E402
from sqlagent.config import get_settings  # noqa: E402
from sqlagent.core.identity import describe, load_dev_users  # noqa: E402
from sqlagent.core.schemas import AgentResponse, Status  # noqa: E402

EXAMPLES = {
    "regional_mgr": ["Net revenue by customer state last year", "Who were our top customers last quarter?",
                     "How many sellers are there in each state?"],
    "exec": ["What was GMV in FY2018?", "Top 5 product categories in English by net revenue this year"],
    "analyst": ["Monthly net revenue trend for calendar year 2017", "Average review score by customer region last year",
                "Best sellers this year"],
    "hr_analyst": ["Average monthly income by department", "Attrition rate by job role"],
}


@st.cache_resource(show_spinner="Preparing the warehouse and the agent (first start can take a minute)...")
def get_agent():
    s = get_settings()
    ensure_warehouse(s)
    return build_agent(s)


def render(resp: AgentResponse, key: str) -> None:
    badge = {Status.answered: st.success, Status.needs_clarification: st.warning,
             Status.refused: st.error, Status.error: st.error}[resp.status]
    badge(resp.message or resp.status.value)
    if resp.status == Status.needs_clarification and resp.clarification:
        for q in resp.clarification.questions:
            cols = st.columns(max(1, len(q.options)))
            for i, opt in enumerate(q.options):
                if cols[i].button(opt, key=f"{key}-opt-{i}"):
                    st.session_state.pending = opt
                    st.rerun()
        st.caption("Pick an option, or type your own answer below.")
    if resp.columns:
        st.dataframe(pd.DataFrame(resp.rows, columns=resp.columns), use_container_width=True, hide_index=True)
        st.caption(f"{resp.row_count} row(s) · {resp.latency_ms / 1000:.1f}s · {resp.attempts} SQL attempt(s)")
    if resp.assumptions:
        with st.expander("Assumptions"):
            for a in resp.assumptions:
                st.markdown(f"- {a}")
    if resp.sql:
        with st.expander("SQL that ran (after the access policy was applied)"):
            st.code(resp.sql, language="sql")
    if resp.metric_definitions:
        with st.expander("Metric definitions"):
            for k, v in resp.metric_definitions.items():
                st.markdown(f"- **{k}**: {v}")


# ---------------------------------------------------------------- sidebar: who is asking
users = load_dev_users()
tokens = list(users)
with st.sidebar:
    st.header("Signed in as")
    choice = st.selectbox("Demo user", tokens, format_func=lambda t: f"{users[t].user_id} · {describe(users[t])[0]}",
                          label_visibility="collapsed")
    user = users[choice]
    st.caption(describe(user)[1])
    s = get_settings()
    st.caption(f"Model: {s.llm_provider} · {s.resolved_llm_model}")
    if st.button("New conversation", use_container_width=True) or st.session_state.get("user") != choice:
        st.session_state.update(user=choice, history=[], conversation_id=None)

st.title("Halcyon SQL Copilot")
st.write("Ask a question about orders, revenue, customers, sellers, reviews or HR. "
         "Your access level decides what you can see.")

agent = get_agent()
history = st.session_state.setdefault("history", [])

if not history:
    st.write("Try one of these:")
    cols = st.columns(len(EXAMPLES.get(user.role, EXAMPLES["analyst"])))
    for i, q in enumerate(EXAMPLES.get(user.role, EXAMPLES["analyst"])):
        if cols[i].button(q, key=f"ex-{i}", use_container_width=True):
            st.session_state.pending = q
            st.rerun()

for i, turn in enumerate(history):
    with st.chat_message(turn["role"]):
        if turn["role"] == "user":
            st.write(turn["text"])
        else:
            render(AgentResponse.model_validate(turn["response"]), key=f"t{i}")

question = st.chat_input("Ask a question in plain English") or st.session_state.pop("pending", None)
if question:
    history.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner("Working on it... a local model can take a minute or two"):
            try:
                resp = agent.ask(question, user, st.session_state.get("conversation_id") or uuid.uuid4().hex)
            except Exception as e:  # noqa: BLE001 - show configuration problems in the page
                st.error(f"The assistant could not answer: {e}. Check that the LLM is reachable "
                         f"({s.llm_provider} · {s.resolved_llm_model}).")
                st.stop()
        st.session_state.conversation_id = resp.conversation_id
        history.append({"role": "assistant", "response": resp.model_dump(mode="json")})
        render(resp, key=f"t{len(history) - 1}")
