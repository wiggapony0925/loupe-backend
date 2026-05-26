.PHONY: install run worker test lint fmt typecheck migrate revision fresh-db docker-up docker-down ocr-eval

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:
	python3.12 -m venv $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

run:
	$(PY) run.py

worker:
	$(VENV)/bin/arq app.worker.WorkerSettings

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check .

fmt:
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check . --fix

typecheck:
	$(VENV)/bin/mypy app

migrate:
	$(VENV)/bin/alembic upgrade head

revision:
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

fresh-db:
	$(VENV)/bin/alembic downgrade base
	$(VENV)/bin/alembic upgrade head

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down -v

# Run the OCR evaluation harness against the configured provider.
# Override with `make ocr-eval PROVIDER=google_vision`.
ocr-eval:
	$(PY) scripts/ocr_eval.py $(if $(PROVIDER),--provider $(PROVIDER),)
