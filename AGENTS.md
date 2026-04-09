# Workflow

- This is a `jj` repo. Do not use `git` to work on the repo itself.
- Do not use git worktree-based agent isolation in this repo. Git worktrees
  branch from git commits and do not carry the live jj operation state, so
  subagents can silently miss uncommitted jj changes. For isolated parallel
  work, use `jj workspace` instead.
- Run the CLI locally with `uv run jj-review ...` instead of invoking the
  module or virtualenv path directly.
- Hard-wrap new prose at 96-98 columns unless the file uses a different
  convention.

# Behaviour changes

- In user-facing output, identify revisions by `change_id` by default. If a
  concrete immutable snapshot matters, include the `commit_id` second and
  label it explicitly.
- Read [docs/notes/design.md](docs/notes/design.md) and
  [docs/notes/implementation-strategy.md](docs/notes/implementation-strategy.md)
  before changing behavior or adding tests. `design.md` is the canonical
  product spec.
- Preserve the core invariants: the `jj` DAG is the source of truth, local
  cache is sparse, GitHub pull requests are derived from the local `jj` stack,
  and ambiguous linkage fails closed.
- If behavior changes, update the docs in the same change and make sure tests
  pass.
- Once a slice is implemented, update the implementation doc to note this.
- Non-blocking design debt, architecture follow-ups, and deferred ideas belong
  in [docs/notes/backlog.md](docs/notes/backlog.md).

# Testing

- Run `./check.py` for the default local Ruff, type-check, and test pass before
  finishing a change.
- For focused test runs, do not use plain `uv run pytest ...`; it can miss the
  repo's package path in this project layout. First run `uv sync --locked`, then
  invoke pytest through the repo virtualenv, for example
  `.venv/bin/python -m pytest tests/unit/test_jj_client.py`.
- When adding, removing, or evaluating tests, read
  [docs/notes/testing-philosophy.md](docs/notes/testing-philosophy.md) first and
  follow it.
