install:

	pip install -r deps/requirements.txt

install-test:
	pip install -r deps/requirements-test.txt

test-coverage:
	python -m pytest test/ -v --cov=. --cov-report=html --cov-report=term

test-collector:
	python -m pytest test/test_collector.py -v

test-seeder:
	python -m pytest test/test_seeder.py -v

test-verifier:
	python -m pytest test/test_verifier.py -v

test-reporter:
	python -m pytest test/test_reporter.py -v

test-rule-controller:
	python -m pytest test/test_rule_controller.py -v

test: install-test test-collector test-seeder test-verifier test-reporter test-rule-controller

lint:
	@black -t py312 --check . --exclude .venv

lint-fix:
	@black -t py312 . --exclude .venv

check: lint

fix: lint-fix

.PHONY: help
help:
	@echo "Usage: make <target>"
	@echo "Targets:"
	@echo "  install: Install dependencies"
	@echo "  install-test: Install test dependencies"
	@echo "  test: Run tests"
	@echo "  test-coverage: Run tests with coverage"
	@echo "  test-collector: Run collector tests"
	@echo "  test-rule-controller: Run rule controller tests"
	@echo "  test-seeder: Run seeder tests"
	@echo "  test-verifier: Run verifier tests"
	@echo "  test-reporter: Run reporter tests"
	@echo "  check: Run lint and tests"
	@echo "  lint: Run lint"
	@echo "  lint-fix: Run lint fix"
	@echo "  fix: Run lint fix"
	@echo "  help: Show this help message"
