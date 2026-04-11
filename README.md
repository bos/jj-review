# jj-review

`jj-review` sends a linear stack of local `jj` commits to GitHub as a stack of
small dependent pull requests. (`jj` is the [Jujutsu version control
system](https://www.jj-vcs.dev/).)

If you want reviewers to see a feature in clear steps instead of one giant PR,
but you do not want to manage a branch per step by hand, this tool is for you.
It is built for rewrite-heavy review: split a feature into a few commits, keep
editing those commits locally in `jj`, and let `jj-review` keep the matching
GitHub PR stack up to date.

## Why you might want this

GitHub pull requests are all right when your work fits into one branch and one
PR.  They get awkward once you want your work to be reviewed piece by piece:

- first refactor the shared model
- then add the API on top
- then add the UI on top of that

While you can do this with plain Git branches, it's kind of a pain. Rebases
churn branch heads, dependent PRs rapidly get confusing, and it's no fun to find
yourself spending more energy maintaining branch plumbing than describing the
actual changes you want reviewed.

`jj-review` takes a different approach. Since `jj` already knows the shape of
your local stack, this tool reads that stack and creates or updates the right
GitHub PRs for it.

## What this looks like

Instead of one large PR:

```text
PR 3: add UI for the new workflow
  base -> PR 2

PR 2: add the workflow API
  base -> PR 1

PR 1: refactor the shared model
  base -> main
```

A reviewer can read the stack from bottom to top, and review the parts they
understand in more or less any order. You, the author, can revise (or add, or
remove) any step locally and re-run one command to refresh the whole stack on
GitHub.

## How `jj-review` is different

- We use the local `jj` stack as the source of truth instead of asking you to
  maintain a parallel branch-management system by hand.
- We create one GitHub PR per reviewable change (not one PR per arbitrary branch
  boundary).
- `jj-review` is designed for the normal `jj` workflow of fluidly rewriting,
  reordering, and restacking commits during review.
- It stays focused on GitHub review instead of trying to become a full hosted
  stack-management platform.

## Who this is for

This tool is a good fit if:

- You already use `jj`, or you are willing to try it
- You already use GitHub pull requests
- You often wish one feature could be reviewed as 2-5 small PRs instead of one
  large chunk
- You want stacked review without paying the cost of branch-per-PR bookkeeping

In fairness, it's likely not for you if:

- You want a plain Git workflow
- You mostly work in single-commit or single-PR changes
- You need support for non-linear review graphs

## Quick start

### What you need

- Python 3.11 or newer
- `uv`
- `jj` 0.39.0 or newer
- GitHub authentication via `gh auth login` or a `GITHUB_TOKEN`

If you are new to `jj`: it is a Git-compatible VCS that makes it much easier to
edit and move around a stack of commits than plain Git. `jj-review` assumes
your local work is already in a `jj` repo.

### Install

The package is published on PyPI:

```bash
uv tool install jj-review
```

To update a PyPI install later:

```bash
uv tool upgrade jj-review
```

After installing, if `jj-review` is not on your shell `PATH`, run:

```bash
uv tool update-shell
```

### Minimal setup

For many GitHub repos, you won't need any configuration. `jj-review` derives
the selected remote, GitHub repository, and trunk branch from the repo state
and fails closed if that resolution is ambiguous.

Repository-level `jj-review` config is only for tool-specific defaults such as
reviewers and labels:

```toml
[jj-review.repo]
reviewers = ["octocat"]
labels = ["needs-review"]
```

For authentication, `jj-review` checks `GH_TOKEN`, then `GITHUB_TOKEN`, then
falls back to `gh auth token` if the GitHub CLI is installed and authenticated.

## Try it in five minutes

Suppose you are in a `jj` workspace wired to talk to a GitHub repo, with a few
local commits stacked on top of `main`.

Check what `jj-review` sees:

```bash
jj-review status
```

Send the current stack to GitHub:

```bash
jj-review submit --current
```

Check the resulting stack again:

```bash
jj-review status
```

You should now have one GitHub pull request per reviewable local change, stacked
in order.

That is the core loop. Edit the commits locally in `jj`, run
`jj-review submit --current` again, and the matching PR stack gets updated.

## A day-one workflow

Here is the workflow this tool is built around:

1. Split a feature into a few small commits in `jj`.
2. Run `jj-review submit --current`.
3. This will give you and reviewers one GitHub PR per change, stacked from
   `main` upward.
4. Revise your commits locally as reviews come in.
5. Re-run `jj-review submit --current`. This will update the PRs.
6. Land the ready parts with `jj-review land --current`.

The nice part is that you get to keep thinking in terms of local logical
changes, not a maze of long-lived Git branches.

## The commands you will actually use

- `jj-review submit --current`
  Create or update the GitHub PR stack for the current `jj` stack.
- `jj-review status`
  Inspect the review status of the current stack. The default output starts
  with capped submitted and unsubmitted summaries, then prints the trunk/base
  row; the submitted summary header links to the newest submitted PR when one
  is available. Summary entries now reuse the user's normal `jj log`
  formatting and colors, with the review status appended to the first rendered
  line. Use `jj-review status --verbose` to expand those summary sections.
  Interactive terminals also show a progress bar while GitHub inspection runs.
- `jj-review land --current`
  Preview what can be landed now.
- `jj-review land --current`
  Land the ready prefix of the current stack.
- `jj-review close --current`
  Preview closing the current stack on GitHub.
- `jj-review close --current`
  Close the tracked PRs for the current stack, and use `--cleanup` to also
  remove verified review branches, local bookmarks, and saved jj-review state.
- `jj-review cleanup`
  Inspect stale saved state, stack comments, old local `review/*` bookmarks,
  or old review branches.
- `jj-review import --pull-request <number-or-url> --fetch`
  Attach local `jj-review` tracking to an existing PR stack.

Advanced repair and shell-integration commands such as `relink`, `unlink`, and
`completion` are available through `jj-review help --all`.

## Current scope

`jj-review` is intentionally focused:

- GitHub only
- linear stacks only
- one PR per reviewable change
- one repository target at a time

Think of it as opinionated tooling for people who actively want a better
stacked-review workflow and are happy to stay within those boundaries.

## Contributor notes

If you are here to work on the tool itself rather than use it:

- local development entrypoint: `uv run jj-review ...`
- default verification command: `./check.py`
- install a pinned release binary for compatibility checks with
  `tools/install-jj-release.sh <version>`
- design doc: [docs/notes/design.md](docs/notes/design.md)
- implementation notes:
  [docs/notes/implementation-strategy.md](docs/notes/implementation-strategy.md)
