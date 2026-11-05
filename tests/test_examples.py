"""Few-shot examples must be correct, permitted for their role, and distinct from the eval set."""
from pathlib import Path

import yaml

from sqlagent.retrieval import load_examples, tokenize
from sqlagent.schemas import UserContext

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = load_examples(ROOT / "config" / "examples.yaml")


def test_examples_pass_guardrails_and_execute(engine, executor):
    assert len(EXAMPLES) >= 8
    for e in EXAMPLES:
        r = engine.check(e.sql, UserContext(user_id="t", role=e.role))
        assert r.allowed, (e.question, r.violations)
        res = executor.run(r.enforced_sql, 1000, 30)
        assert hasattr(res, "rows"), (e.question, res)


def test_examples_do_not_leak_eval_questions():
    cases = yaml.safe_load((ROOT / "eval" / "cases.yaml").read_text())["cases"]
    eval_q = {" ".join(tokenize(c["question"])) for c in cases}
    for e in EXAMPLES:
        assert " ".join(tokenize(e.question)) not in eval_q, e.question


def test_similar_examples_are_role_filtered(catalog, glossary, policy):
    from sqlagent.retrieval import Retriever
    r = Retriever(catalog, glossary, EXAMPLES)
    shots = r.similar_examples("average monthly income by department", policy.role("analyst"))
    assert all(not s.question.lower().startswith(("average monthly income", "headcount")) for s in shots)
    shots = r.similar_examples("average monthly income by department", policy.role("hr_analyst"))
    assert shots and "income" in shots[0].question.lower()
