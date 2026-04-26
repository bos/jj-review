# jj-review

`jj-review` sends a linear stack of local `jj` changes to GitHub as a stack of
small dependent pull requests.

It is built for a rewrite-heavy review workflow. Split a feature into a few local changes, keep
editing those changes in `jj`, and let `jj-review` keep the matching GitHub PR stack up to date.

## Why use it

Vanilla GitHub review gets awkward once one feature really wants to be reviewed in a series of
steps. For example:

- first refactor the shared model
- then add the API on top
- then add the UI on top of that

You can model that with plain Git branches, but the bookkeeping quickly becomes unwieldy.
`jj-review` takes a different approach:

- the local `jj` DAG is the source of truth for the stack
- each change gets one review branch and one PR
- mutable history stays in `jj`, not in a parallel branch-management layer

Those review branches show up locally as `jj` bookmarks with a configurable prefix. By default
they use `review/...`, but a repo can pick another prefix such as `bosullivan/...`. They are the
GitHub PR head branches. `jj-review` manages them for you, so most of the time you do not need
to think about them directly.

If you already use `jj` and GitHub pull requests, and you often want a series of small PRs
instead of one large one, this tool is likely a good fit.

## Performance

The GitHub API is *slow*; a single roundtrip takes hundreds of milliseconds. `jj-review` reduces
its impact in the best ways I could find:
- GraphQL batch requests where possible
- Concurrent use of the GitHub REST API

(Early versions started out naively using the REST API serially, and they were horrendously
slow. The tool as it stands is now tens of times faster on stacks of even modest size.)

## Why should your agent use it?

A typical 2026-era coding agent will barf out a giant hairball of code in one convulsive glob,
given the opportunity. How can `jj-review` help manage this?

Any reviewer, human or not (and you *do* actually review code, right?) is going to have a vastly
easier time with a series of smaller, self-contained increments. So at least from the *demand*
side, the benefit of a tool like this is clear.

- Agents work best when tasks are decomposed. A stacked review lets an agent revise only those
  commits that are wrong (and their descendants, as required) then resubmit, instead of
  reopening one enormous PR where review-driven changes get lost in the maelstrom.

- Smaller PRs are far easier for both humans and agents to re-read after feedback. Context
  windows are bigger in 2026, but agent attention is still limited, and human attention is under
  ever more strain.

- Validation is more easily staged. It's easier to approve and land good-to-go changes while
  others are still in flux.

- Mutable local history is more valuable with agents. You've seen how an agent will often
  produce a first draft that needs aggressive reshaping. `jj` is the best tool to rework changes
  and history, and `jj-review` offers the smoothest way to get the cleaned-up result to GitHub.

## But *ew*, it was written by an agent!

If you simply *must* clutch your pearls because of a tool written by AI, then this one offers
you a rich vein of offense that is yours to take.

- I haven't written a single line of code (user-facing docs? different story)
- Despite attention to the internals, the code is more chaotic than I enjoy

However:

- The user experience is solid
- The test suite is good: around 450 tests with >85% coverage as of April 2026
- I was able to vastly improve performance without losing my mind to the depravities of GraphQL
  or async Python

## Quick start

### Requirements

- Python 3.14 or newer
- `uv`
- `jj` 0.39.0 or newer
- GitHub authentication via `gh auth login` or a `GITHUB_TOKEN`

### Install

```bash
uv tool install jj-review
```

To upgrade later:

```bash
uv tool upgrade jj-review
```

If `jj-review` is not on your shell `PATH`, run:

```bash
uv tool update-shell
```

### Two-minute first run

Suppose you are already in a `jj` repo, and it's hooked up to GitHub, and you have a few local
commits stacked on top of `main`. Getting started is super easy.

Inspect your current stack:

```bash
jj-review
```

This defaults to `jj-review status`.
`status` also accepts the short alias `st`.

Inspect every locally tracked stack in the repo:

```bash
jj-review list
```

`list` also accepts the short alias `ls`.

Submit that stack to GitHub:

```bash
jj-review submit
```

`submit` also accepts the short alias `sub`.

When you first submit a stack, this will create one review bookmark per change (by default
`review/...`; these are managed automatically). Those bookmarks are user-visible in `jj`, and
managed by `jj-review`.

Inspect your stack again:

```bash
jj-review
```

At that point you should have one PR per local change, in a stack (each one based on its
predecessor). Edit your changes locally in `jj`, run `jj-review submit` again, and the PR stack
will be refreshed.

## Core workflow

Your typical author loop will be dead simple:

1. Write code as a series of local `jj` changes.
2. Run `jj-review submit`.
3. Revise those changes locally as reviews come in.
4. Re-run `jj-review submit`.
5. Once some PRs are approved, rebase if needed, then run `jj-review land` to push those exact
   local commits to GitHub trunk and forget the local review bookmarks for the landed changes.
6. Run `jj-review cleanup --rebase` only if lower changes were merged through different commit
   IDs and your local stack still contains those merged ancestors. After that local rewrite, run
   `jj-review submit` to refresh the surviving PRs on GitHub.

The key point is that you get to keep thinking in terms of local logical changes. `jj-review`
manages those changes on GitHub, does some housekeeping for you locally, and that's it.

When you are juggling more than one local review stack in the same repo, run
`jj-review list` to see the locally tracked stacks at a glance before drilling
into one of them with `jj-review status`.

One piece of that housekeeping is the review bookmark set. Those bookmarks are the review
branches pushed to GitHub for each change. You may see them in `jj log` or `jj bookmark list`,
but you generally should not move or rename them by hand unless you are doing explicit repair
work.

## Learn More

User guides live under [docs](docs/README.md):

- [Mental model](docs/mental-model.md)
- [Daily workflow](docs/daily-workflow.md)
- [Troubleshooting](docs/troubleshooting.md)

I've written what I hope is comprehensive built-in help.

```bash
jj-review --help
jj-review submit --help
```

A few housekeeping commands are hidden by default.

```bash
jj-review help --all
```

Like `jj`, `jj-review` also accepts `--color=always|never|debug|auto`. Without
that flag, it follows your `jj` `ui.color` setting.

## The lower bound of configuration is zero

For most use, `jj-review` needs no configuration. It derives `git`, `jj`, and GitHub information
directly from `git`, `jj`, and `gh` whenever possible.

Repo-level config can be helpful for defaults such as reviewers and labels:

```toml
[jj-review]
bookmark_prefix = "bosullivan"
reviewers = ["octocat"]
labels = ["needs-review"]
use_bookmarks = ["potato/*", "spam/eggs"]
```

If you leave `bookmark_prefix` unset, `jj-review` keeps the default
`review/...` prefix.

`jj-review submit` can override those defaults with `--reviewers`,
`--team-reviewers`, `--label`, and `--use-bookmarks`.

`cleanup_user_bookmarks` defaults to `false`. Leave it unset if bookmarks selected through
`use_bookmarks` should be preserved during later cleanup. Set it to `true` only if you want
`cleanup`, `close --cleanup`, and `land` to delete those reused bookmarks too when that cleanup
is otherwise safe.

For authentication, `jj-review` checks `GH_TOKEN`, then `GITHUB_TOKEN`, then falls back to `gh
auth token` if `gh`, the GitHub CLI, is installed and authenticated.

## Scope: narrow

`jj-review` is intentionally focused:

- `jj` has best-in-class mutable history
- `jj-review` is GitHub only (at least for now)
- linear stacks only (ever tried reviewing a DAG of changes? no thx)
- one PR per change ID

Other tools that layer stacked reviews on top of GitHub are either super minimal, or are based
on `git` and have to add a ton of history mangling commands to their UIs.

In late 2025, GitHub's CTO said they were working on stacked review support. If that ever
launches, `jj-review` should be able to easily accommodate it.
