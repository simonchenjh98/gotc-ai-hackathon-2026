PYTHON ?= python3
NODE ?= node
PORT ?= 5274

.PHONY: dev check test frontend-test backend-test operations-test benchmark

dev:
	GOTC_PORT=$(PORT) GOTC_PUBLIC_ORIGIN="http://127.0.0.1:$(PORT)" $(PYTHON) -B -m backend.gotc

check:
	$(PYTHON) -B tools/check_layout.py
	$(PYTHON) -B tools/check_site.py

test: check frontend-test backend-test operations-test

frontend-test:
	$(NODE) --test tests/frontend/*.test.mjs

backend-test:
	$(PYTHON) -B -m unittest discover -s tests/backend -p 'test_*.py'

operations-test:
	$(PYTHON) -B -m unittest discover -s tests/operations -p 'test_*.py'

benchmark:
	$(PYTHON) -B tests/performance/standings.py
