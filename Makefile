.PHONY: install data-kaggle data-sample warehouse test eval-oracle eval api cli lint docker

install:        ## install with dev extras
	pip install -e ".[dev,kaggle]"

data-kaggle:    ## download real data from Kaggle (needs ~/.kaggle/kaggle.json)
	python setup/download_kaggle.py

data-sample:    ## offline / CI: seeded synthetic data with the Kaggle schemas
	python setup/make_sample_data.py

warehouse:      ## build data/warehouse.duckdb from data/raw
	python setup/build_warehouse.py

lint:
	ruff check .

test:
	pytest -q

eval-oracle:    ## pipeline self-test, no LLM needed
	python eval/run_eval.py --mode oracle --gate

eval:           ## live eval with the configured LLM
	python eval/run_eval.py --baseline eval/reports/last-live.json

api:
	uvicorn sqlagent.api:app --reload --port 8000

cli:
	python -m sqlagent.cli --role regional_mgr --regions SE "top customers last quarter"

docker:
	docker compose build
