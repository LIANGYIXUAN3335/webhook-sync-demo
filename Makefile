# Convenience targets. Everything here is plain commands you can also run by hand.
PY      ?= python3
VENV    := .venv
BIN     := $(VENV)/bin
HOST    ?= 127.0.0.1
PORT    ?= 8000

.PHONY: help setup run test lint format demo reset clean

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

setup:  ## Create .venv (Python 3.11+), install dev dependencies, create .env from the example
	@if command -v uv >/dev/null 2>&1; then \
		uv venv -q --python 3.12 $(VENV) && uv pip install -q --python $(BIN)/python -r requirements-dev.txt; \
	else \
		$(PY) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
			{ echo "Python 3.11+ required: run 'make setup PY=python3.12' (or install uv)"; exit 1; }; \
		$(PY) -m venv $(VENV) && $(BIN)/pip install -q -r requirements-dev.txt; \
	fi
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

run:  ## Start the app (dashboard at http://127.0.0.1:8000)
	$(BIN)/uvicorn app.main:create_app --factory --host $(HOST) --port $(PORT)

test:  ## Run the test suite
	$(BIN)/python -m pytest -v

lint:  ## Lint and check formatting
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Auto-format
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

demo:  ## Run every demo scenario against the running app (resets demo data)
	$(BIN)/python -m scripts.demo_all --yes --url http://$(HOST):$(PORT)

reset:  ## Reset demo data on the running app
	curl -s -o /dev/null -w "reset: HTTP %{http_code}\n" -X POST http://$(HOST):$(PORT)/demo/reset

clean:  ## Remove the local database and caches
	rm -rf data .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
