.PHONY: install data data-mirror data-sample warehouse lint test test-unit test-security eval-oracle eval api cli explorer docker

install:          ## editable install with dev extras
	pip install -e ".[dev,kaggle]"

data:             ## real data from Kaggle (needs ~/.kaggle/kaggle.json)
	halcyon-warehouse download

data-mirror:      ## same Kaggle files from pinned, checksum-verified GitHub mirrors
	halcyon-warehouse download --source github-mirror

data-sample:      ## offline / CI: seeded synthetic data with the Kaggle schemas
	halcyon-warehouse sample

warehouse:        ## build data/warehouse.duckdb from data/raw
	halcyon-warehouse build

lint:
	ruff check .

test:
	pytest -q

test-unit:
	pytest -q tests/unit

test-security:
	pytest -q tests/security

eval-oracle:      ## pipeline self-test, no LLM needed (release gate)
	sqlagent-eval --mode oracle --gate

eval:             ## live eval with the configured LLM
	sqlagent-eval --baseline evals/reports/last-live.json

api:              ## API + web UI on http://localhost:8000
	sqlagent-api --port 8000

cli:
	sqlagent --role regional_mgr --regions SE "top customers last quarter"

explorer:         ## regenerate docs/data-explorer.html
	python scripts/build_data_explorer.py

docker:
	docker build -f deploy/Dockerfile -t halcyon-sql-copilot .
