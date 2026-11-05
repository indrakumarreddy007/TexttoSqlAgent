"""The eval harness must be trustworthy: the comparator must catch wrong answers, and the
oracle run (gold answers replayed through the full pipeline) must score 100%."""
import sys
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import InMemorySaver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402

from sqlagent.agent import Agent  # noqa: E402
from sqlagent.config import Settings  # noqa: E402
from sqlagent.graph import Services  # noqa: E402
from sqlagent.llm import ScriptedLLM  # noqa: E402
from sqlagent.resolver import Resolver  # noqa: E402
from sqlagent.retrieval import Retriever  # noqa: E402


def test_comparator_accepts_reordered_and_renamed_columns():
    ok, _ = run_eval.results_match(["n", "state"], [[2, "RJ"], [5, "SP"]], ["state", "orders"],
                                   [["SP", 5], ["RJ", 2]])
    assert ok


def test_comparator_rejects_wrong_values_counts_and_order():
    assert not run_eval.results_match(["s", "n"], [["SP", 5]], ["s", "n"], [["SP", 6]])[0]
    assert not run_eval.results_match(["s"], [["SP"]], ["s"], [["SP"], ["RJ"]])[0]
    assert not run_eval.results_match(["s", "n"], [["RJ", 2], ["SP", 5]], ["s", "n"], [["SP", 5], ["RJ", 2]],
                                      ordered=True)[0]


def test_comparator_numeric_tolerance():
    assert run_eval.results_match(["v"], [[100.004]], ["v"], [[100.0]])[0]
    assert not run_eval.results_match(["v"], [[101.0]], ["v"], [[100.0]])[0]


def test_oracle_eval_scores_100_percent(warehouse, catalog, glossary, policy, engine, executor, tmp_path):
    svc = Services(Settings(warehouse_path=warehouse), catalog, glossary, policy, engine,
                   Retriever(catalog, glossary), Resolver(glossary), executor, ScriptedLLM())
    agent = Agent(svc, InMemorySaver())
    cases = yaml.safe_load((ROOT / "eval" / "cases.yaml").read_text())["cases"]
    results = []
    for c in cases:
        agent.svc.llm = run_eval.oracle_llm(c)
        results.append(run_eval.run_case(agent, c, executor))
    summary = run_eval.summarize(results)
    failures = [(r["id"], r.get("exec_detail"), r["message"]) for r in results if not r["pass"]]
    assert summary["pass_rate"] == 1.0, failures
    assert summary["unsafe_answers"] == 0
    assert not run_eval.check_gates(summary)


def test_gold_answers_are_not_trivially_empty(executor):
    cases = yaml.safe_load((ROOT / "eval" / "cases.yaml").read_text())["cases"]
    for c in cases:
        if c.get("gold_sql"):
            res = executor.run(c["gold_sql"], 10_000, 30)
            assert hasattr(res, "rows") and res.rows, c["id"]
