python := if os() == "windows" { ".venv/Scripts/python.exe" } else { ".venv/bin/python" }

alias fmt := format
alias web := website

# List the available development workflows.
default:
    @just --list

# Install the locked development environment.
setup:
    uv sync --locked

# Run jj-stack from this checkout.
run *args:
    uv run jj-stack {{args}}

# Refresh the sibling website's generated jj-stack documentation snapshot.
website:
    cd ../website && JJ_STACK_SOURCE=../jj-stack scripts/sync-jj-stack-docs.py

# Check whether the sibling website's jj-stack documentation snapshot is current.
website-check:
    cd ../website && JJ_STACK_SOURCE=../jj-stack scripts/sync-jj-stack-docs.py --check

# Apply the repository's Ruff fixes and formatting.
format: setup
    {{python}} -m ruff check --fix
    {{python}} -m ruff format

# Run the standard Ruff, type-check, and test pass.
check *args:
    ./check.py {{args}}

# Run a focused pytest selection after refreshing the environment.
test *args: setup
    {{python}} -m pytest {{args}}

# Check the cumulative code and test complexity budgets.
complexity:
    uv run tools/check_complexity.py

# Run the opt-in generated submit scenarios; arguments pass through to the runner.
property *args:
    tests/run_submit_property_scenarios.py {{args}}

# Run the opt-in release checks against a disposable real GitHub repository.
live *args:
    uv run python tests/run_live_github.py {{args}}

# Build the wheel and source distribution.
build:
    uv build

# Build and smoke-test both release artifacts outside the source tree.
artifact-check: build
    uv run --no-project --python 3.14 python tools/check_release_artifacts.py

# Run all local release qualification gates.
release-check: check complexity live
