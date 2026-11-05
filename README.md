# Halcyon SQL Copilot

An agentic text-to-SQL service for a governed, enterprise-style warehouse. Business users ask
incomplete questions in English. The agent works out what they meant, asks when it cannot safely
guess, writes SQL, and proves before running anything that the query obeys access, privacy and
cost policy.

- **Design (HLD):** [`docs/architecture.html`](docs/architecture.html)
- **Stack (all free / open source):** LangGraph, sqlglot, DuckDB, FastAPI, Pydantic, Ollama
  (`qwen2.5-coder:7b`), rank-bm25, Faker, pytest + hypothesis, Langfuse (optional), Docker,
  GitHub Actions
- **Data:** Kaggle [Olist Brazilian E-Commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
  and [IBM HR Analytics](https://www.kaggle.com/datasets/pavansubhasht/ibm-hr-analytics-attrition-dataset),
  reshaped into an enterprise warehouse with the usual problems: cryptic names, soft deletes, SCD2
  history, a deprecated table, BRL amounts with FX, an April fiscal year, Portuguese categories and
  synthetic PII

## Quickstart

```bash
pip install -e ".[dev,kaggle]"
```

Get the data. Use real Kaggle data with a token in `~/.kaggle/kaggle.json`:

```bash
python setup/download_kaggle.py
```

If Kaggle is blocked on your network, download the same files from pinned GitHub mirrors (each
file is SHA-256 verified and matches the Kaggle row counts):

```bash
python setup/download_kaggle.py --source github-mirror
```

Or, offline or in CI, generate seeded data with the same schemas:

```bash
python setup/make_sample_data.py
```

Build the warehouse (about 5 seconds for the sample data, about 12 seconds for the full Kaggle data):

```bash
python setup/build_warehouse.py
```

Start a local LLM (free):

```bash
ollama pull qwen2.5-coder:7b
```

Ask a question:

```bash
python -m sqlagent.cli --role regional_mgr --regions SE "who were our top customers last quarter?"
```

Or run the API:

```bash
uvicorn sqlagent.api:app --port 8000
```

```bash
curl -s localhost:8000/v1/query -H "Authorization: Bearer dev-mgr-se-token" -H "Content-Type: application/json" -d '{"question": "top customers last quarter"}'
```

Answer a clarification by sending the reply with the same `conversation_id`.

## How it works

```
input_guard -> retrieve_context -> qualify (LLM) -> resolve_spec -> [clarify: interrupt()]
  -> generate_sql (LLM) -> validate_and_enforce -> cost_check -> execute -> output_guard -> answer
       ^____________ correctable violations, at most 3 attempts ___________|
  -> refuse
```

| Module | Responsibility |
|---|---|
| `sqlagent/graph.py` | LangGraph `StateGraph`; routers are pure functions |
| `sqlagent/guardrails/engine.py` | **Security boundary.** AST checks + policy-view rewrite (masking, row-level security, k-anonymity, LIMIT cap) |
| `sqlagent/input_guard.py` | Injection, write intent, role claims, requests for restricted data |
| `sqlagent/retrieval.py` | BM25 + foreign-key join paths, filtered by role |
| `sqlagent/resolver.py` | Fiscal-calendar date resolution, glossary defaults, clarify-or-proceed decision |
| `sqlagent/llm.py` | Provider-neutral structured output (Ollama / Anthropic / OpenAI-compatible); `ScriptedLLM` for tests |
| `sqlagent/executor.py` | Read-only DuckDB, external access off, timeout, row cap, `EXPLAIN` cost check |
| `sqlagent/audit.py` | Append-only JSONL audit log with a per-node trace |
| `config/*.yaml` | Catalog, glossary (certified metrics), policy as code, dev identities |

The LLM never enforces policy. Every base table in generated SQL is replaced by a policy view:
permitted columns only, masks applied, deleted rows removed, the caller's region filter injected.
That happens regardless of what the model wrote.

## Roles (`config/policy.yaml`)

| Role | Scope |
|---|---|
| `exec` | All commercial data; customer PII masked |
| `regional_mgr` | Commercial data for own regions only (row-level security); no PII |
| `analyst` | Commercial data, all regions; no PII, no free text |
| `hr_analyst` | HR data; names masked; pay is aggregate-only with minimum group size 5 |
| `admin` | Unrestricted; internal use for eval gold answers |

Dev tokens are in `config/users.yaml`. In production, replace `api.resolve_user` with JWT validation.

## Testing and evaluation

```bash
pytest -q
```

That runs 122 tests: data invariants, catalog consistency, a security corpus of about 60 hostile
queries, execution-level policy checks, a hypothesis property test for row-level security, every
graph route with a scripted LLM, and the API contract.

```bash
python eval/run_eval.py --mode oracle --gate
```

That replays the gold answers through the full pipeline without an LLM. It must score 100%.

```bash
python eval/run_eval.py --gate --baseline eval/reports/last-live.json
```

That is the live eval against the configured LLM.

The eval set (`eval/cases.yaml`, 28 cases) covers: well-specified, glossary, underspecified
(clarify), role-scoped, must-refuse and adversarial. Release gates:

| Gate | Threshold |
|---|---|
| Unsafe answers | 0 |
| Behaviour accuracy | ≥ 90% |
| Execution accuracy | ≥ 80% |
| Clarification recall | ≥ 90% |
| Over-clarification | ≤ 10% |

Reports are written to `eval/reports/`.

## Configuration

Settings come from `SQLAGENT_*` environment variables or `.env` (see `.env.example`). To switch
the LLM, set `SQLAGENT_LLM_PROVIDER=anthropic` and `SQLAGENT_LLM_MODEL=<model>`, or use `openai`
with `SQLAGENT_LLM_BASE_URL` for Groq, vLLM or LM Studio.
