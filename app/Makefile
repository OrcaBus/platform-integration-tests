install:

	pip install -r requirements.txt

check: lint

lint:
	@black -t py312 --check . --exclude .venv

lint-fix:
	@black -t py312 . --exclude .venv

fix: lint-fix
