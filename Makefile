PYTHON ?= python3
NODE ?= node
PORT ?= 5274
VENV ?= .runtime/venv
WHEELHOUSE ?= .runtime/wheels
APP_PYTHON := $(VENV)/bin/python
LOCK := backend/requirements.txt

.PHONY: wheels deps dev check test frontend-test backend-test operations-test benchmark

dev: deps
	GOTC_PORT=$(PORT) GOTC_PUBLIC_ORIGIN="http://127.0.0.1:$(PORT)" "$(APP_PYTHON)" -B -m backend.gotc

$(APP_PYTHON):
	"$(PYTHON)" -m venv "$(VENV)"

# Reuse a populated wheelhouse offline, including one copied from another build host.
wheels: | $(APP_PYTHON)
	@mkdir -p "$(WHEELHOUSE)"
	@"$(APP_PYTHON)" -Im pip --disable-pip-version-check download --platform any --no-index --find-links "$(WHEELHOUSE)" --only-binary=:all: --require-hashes -r $(LOCK) -d "$(WHEELHOUSE)" >/dev/null 2>&1 || \
	  "$(APP_PYTHON)" -Im pip --disable-pip-version-check download --platform any --only-binary=:all: --require-hashes -r $(LOCK) -d "$(WHEELHOUSE)"

deps: $(VENV)/.gotc-requirements

$(VENV)/.gotc-requirements: $(LOCK) $(APP_PYTHON) | wheels
	"$(APP_PYTHON)" -Im pip --disable-pip-version-check install --no-index --find-links "$(WHEELHOUSE)" --only-binary=:all: --require-hashes -r $(LOCK)
	@touch "$@"

check:
	$(PYTHON) -B tools/check_layout.py
	$(PYTHON) -B tools/check_site.py

test: check frontend-test backend-test operations-test

frontend-test:
	$(NODE) --test tests/frontend/*.test.mjs

backend-test: deps
	WHEELHOUSE="$(abspath $(WHEELHOUSE))" "$(APP_PYTHON)" -B -m unittest discover -s tests/backend -p 'test_*.py'

operations-test: deps
	WHEELHOUSE="$(abspath $(WHEELHOUSE))" "$(APP_PYTHON)" -B -m unittest discover -s tests/operations -p 'test_*.py'

benchmark: deps
	"$(APP_PYTHON)" -B tests/performance/standings.py
