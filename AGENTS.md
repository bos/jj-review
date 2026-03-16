# Repo Notes

- This is a `jj` repo. Do not use `git` to work on the repo itself.
- Read [docs/notes/design.md](docs/notes/design.md) and
  [docs/notes/implementation-strategy.md](docs/notes/implementation-strategy.md)
  before changing behavior. `design.md` is the canonical product spec.
- Keep the MVP narrow: `submit`, `status`, `sync`, `adopt`, and `cleanup`.
  `land` is post-MVP.
- Preserve the core invariants: the `jj` DAG is the source of truth, local
  cache is sparse, GitHub state is projected state, and ambiguous linkage fails
  closed.
- Run the CLI locally with `uv run jj-review ...` instead of invoking the
  module or virtualenv path directly.
- Run `./check.py` for the default local Ruff, type-check, and test pass before
  finishing a change.
- If behavior changes, update the docs in the same change and make sure tests pass.
- Once a slice is implemented, update the implementation doc to note this.
- Non-blocking design debt, architecture follow-ups, and deferred ideas belong
  in [docs/notes/backlog.md](docs/notes/backlog.md).
- Hard-wrap new prose at 96-98 columns unless the file uses a different
  convention.
