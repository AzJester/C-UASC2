# C-UAS C2 reference scaffold — developer entry points.
# See docs/QUICKSTART.md for the guided walkthrough.

COMPOSE ?= docker compose
PY ?= python3
VENV ?= .venv

.PHONY: help up down logs demo test venv lint validate-specs run-c2 run-sim clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build & start NATS + c2-core + sensor-sim
	$(COMPOSE) up --build -d
	@echo "c2-core API docs: http://localhost:8000/docs"

down: ## Stop and remove the stack
	$(COMPOSE) down -v

logs: ## Tail all service logs
	$(COMPOSE) logs -f

demo: ## Run the scripted any-sensor/any-shooter walkthrough against a running stack
	$(PY) scripts/demo.py

venv: ## Create a local venv and install c2-core + test deps
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r services/c2-core/requirements.txt -r requirements-dev.txt

test: ## Run the unit tests (no broker needed)
	$(VENV)/bin/pytest -q || ( echo "run 'make venv' first if deps are missing" && exit 1 )

validate-specs: ## Validate JSON Schemas / OpenAPI / AsyncAPI structure
	$(VENV)/bin/python scripts/validate_specs.py

lint: ## Byte-compile all Python (cheap syntax check)
	$(PY) -m compileall -q services scripts tests

run-c2: ## Run c2-core locally (needs a reachable NATS or runs degraded)
	$(VENV)/bin/uvicorn app.main:app --app-dir services/c2-core --host 0.0.0.0 --port 8000

run-sim: ## Run the edge simulator locally
	$(VENV)/bin/python services/sensor-sim/sim.py

clean: ## Remove venv and caches
	rm -rf $(VENV) .pytest_cache **/__pycache__
