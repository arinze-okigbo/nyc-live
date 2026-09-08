# nyc-live task runner. `just` with no args lists recipes.

set dotenv-load := true
set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Install everything (core + all workspace packages + dev tools)
sync:
    uv sync --all-packages --group dev

# Lint + types + tests. This is the gate for every phase.
check: lint types test

lint:
    uv run ruff format --check .
    uv run ruff check .

fmt:
    uv run ruff format .
    uv run ruff check --fix .

types:
    uv run pyright

# Offline tests only (live-marked tests are skipped unless NYC_LIVE_TESTS=1)
test *ARGS:
    uv run pytest {{ARGS}}

# Tests including live upstream calls
test-live *ARGS:
    NYC_LIVE_TESTS=1 uv run pytest -m live {{ARGS}}

# One-line status per feed against the real upstreams
smoke:
    uv run nyc-smoke

mcp *ARGS:
    uv run nyc-mcp {{ARGS}}

vision *ARGS:
    uv run nyc-vision {{ARGS}}

dash *ARGS:
    uv run nyc-dash {{ARGS}}

clean:
    rm -rf .pytest_cache .ruff_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
