.PHONY: run test lint

run:
	python -m app.main

test:
	pytest -q

lint:
	ruff check app tests
