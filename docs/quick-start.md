---
title: Quick start
linkTitle: Quick start
description: Install jj-stack, prepare your repo, and submit your first stack.
navGroup: Start here
weight: 10
---

## Requirements

- Python 3.14 or newer
- `jj` 0.45.1 or newer
- a repo on github.com where you can push branches and open pull requests

`jj-stack` uses `GITHUB_TOKEN`, then `GH_TOKEN`, then your GitHub CLI login. If you use the
GitHub CLI and have not signed in, run `gh auth login`.

GitHub stacked pull requests are in
[public preview](https://docs.github.com/en/pull-requests/tutorials/roll-out-stacked-prs) and
require no repo or organization setup.

## Install

Install `jj-stack` from PyPI with [`uv`](https://docs.astral.sh/uv/) in an isolated tool
environment (recommended):

```console
uv tool install jj-stack
```

[`pipx`](https://pipx.pypa.io/) provides another isolated installation:

```console
pipx install jj-stack
```

You can also use `pip` inside an activated virtual environment:

```console
python -m pip install jj-stack
```

To upgrade an installation made with `uv`, run `uv tool upgrade jj-stack`. If your shell cannot
find `jj-stack` after installation, run `uv tool update-shell` and restart your shell.

## Prepare the repo

Inside your `jj` repo, check your remote, GitHub access, and trunk configuration:

```console
jj-stack doctor --fix
```

The `--fix` option also configures `jj git fetch` to skip PR branches and removes untracked PR
bookmarks imported by earlier fetches. This keeps those branches out of your local bookmark view.
Resolve any failed checks before continuing; `doctor` includes guidance in its output.

## Build your local stack

Start with an empty working copy based on `trunk()`. Edit files, then use `jj commit` to describe
each change and start a fresh empty working copy above it:

```console
# edit files
jj commit -m "A: refactor shared model"
# edit files
jj commit -m "B: add API"
# edit files
jj commit -m "C: add UI"
```

You now have three described changes above `trunk()`, with an empty working copy above them:

```text
trunk() <- A <- B <- C <- @ (empty)
```

## Inspect and submit

Run `jj-stack` with no subcommand to see your stack:

```console
jj-stack
```

Because `@` is empty, `jj-stack` selects the described change at `@-` as the top of the stack.

Submit your stack for review:

```console
jj-stack submit
```

`submit` creates one pull request for each change and groups them into a GitHub stack in the
same order. The PR for A targets trunk; B targets A's PR branch, and C targets B's. Reviewers see
only each change's diff in its PR.

In a terminal with hyperlink support, the PR labels in the output open GitHub. Click the PR
beside `Top of stack` to open the top PR; `jj-stack view` offers the same link in its
`Submitted stack` heading, and `jj-stack list` links a count such as `5 PRs` to that PR.
Use your terminal's usual gesture for opening links.

## Revise normally

After submitting, `@` is still empty and sits above `C: add UI`. To revise C, edit files in `@`
and squash those edits into `@-`. You can also reorder the stack with `jj arrange` before
resubmitting:

```console
# edit files
jj squash
jj arrange
jj-stack submit
```

Because C keeps its change ID, `jj-stack` updates its existing pull request. Any reordered changes
must still apply in their new order. See [edit and rearrange a stack](guides/revise.md) for more
examples.

## Invoke it as `jj stack`

If you prefer `jj stack` to `jj-stack`, add this alias with `jj config edit --user`:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

For completion of both `jj-stack` and `jj stack`, add this to `~/.zshrc` after your existing
completion setup:

```zsh
eval "$(jj-stack completion zsh --jj-alias stack)"
```

For bash or fish, see [shell completion](reference/configuration.md#shell-completion).

## What next?

- Read [how jj-stack works](mental-model.md) before restructuring your submitted stack.
- Follow [submit and update](guides/submit-and-update.md) for descriptions, drafts, and reviewer
  requests.
- Read [merge and sync](guides/merge-and-sync.md) before landing your stack.
