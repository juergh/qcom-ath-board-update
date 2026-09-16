all: lint test

lint: ruff mypy

test: unittest

ruff:
	@echo "II: Running ruff"
	uv run python -m ruff format
	uv run python -m ruff check --fix
	@echo

mypy:
	@echo "II: Running mypy"
	uv run python -m mypy .
	@echo

unittest:
	@echo "II: Running unittest"
	uv run python -m unittest discover -v
	@echo

.PHONY: all lint test ruff mypy unittest
