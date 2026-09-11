# Developer entry points.  Windows: run the equivalent commands from README.md.
PY ?= python
PORT ?= 8000

.PHONY: install prepare serve lint format test eval check clean

install:            ## Editable install with dev tooling and git hooks
	$(PY) -m pip install -e ".[dev]"
	pre-commit install

prepare:            ## Build warehouse.duckdb and prep_report.json (one command)
	$(PY) prepare.py

serve:              ## Run the HTTP service on $(PORT)
	PORT=$(PORT) $(PY) -m uvicorn acpl_assistant.service:app --host 0.0.0.0 --port $(PORT)

lint:               ## Ruff lint + format check
	ruff check .
	ruff format --check .

format:             ## Auto-fix and format
	ruff check --fix .
	ruff format .

test:               ## Unit and integration tests with coverage
	pytest --cov --cov-report=term-missing

eval:               ## Run the evaluation set against a running service
	$(PY) eval/run_eval.py

check: lint test    ## Everything CI runs

clean:
	rm -rf .ruff_cache .pytest_cache .coverage htmlcov build dist *.egg-info
	find . -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
