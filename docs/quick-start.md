---
title: Quick start
linkTitle: Quick start
description: Install jj-stack and submit your first stack in a few minutes.
navGroup: Start here
weight: 10
---

## Requirements

- Python 3.14 or newer
- `jj` 0.44.0 or newer
- a GitHub repo you can push to, and GitHub authentication

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

To upgrade an installation made with `uv`, rerun its command with `--force`.

## Prepare the repo

Inside your `jj` repo, prepare it for use:

```console
jj-stack doctor --fix
```

Confirm that the `GitHub stacks` check passes. `doctor` explains how to resolve an unavailable
Stacks API before `submit` pushes anything.

## Build your local stack

Treat `@` as a scratch working copy. Edit files, then use `jj commit` to finish each change and
start a fresh empty `@` on top:

```console
# edit files
jj commit -m "A: refactor shared model"
# edit files
jj commit -m "B: add API"
# edit files
jj commit -m "C: add UI"
```

You now have three described changes above `trunk()`, with a new empty working copy above them.
Keep using ordinary `jj` commands to create and rearrange your local changes. `jj-stack` will take
care of the GitHub side.

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

`submit` creates one pull request for each of your changes, links your pull requests in the same
order, and creates your stack on GitHub.

## Revise normally

After submitting, `@` is still the empty scratch working copy above the `C: add UI` change. To
revise that change, edit files in `@` and squash those edits into `@-`. You can also rearrange the
stack before resubmitting:

```console
# edit files
jj squash
jj arrange
jj-stack submit
```

`jj squash` moves the working-copy changes into `@-`. Because the `C: add UI` change keeps its
change ID, `jj-stack` updates its existing pull request instead of opening a new one. After
`jj arrange` reorders the stack, `jj-stack` updates the existing pull requests to match.

## What next?

- Read [how jj-stack works](mental-model.md) before restructuring your submitted stack.
- Follow [submit and update](guides/submit-and-update.md) for descriptions, drafts, and reviewer
  requests.
- Read [merge and sync](guides/merge-and-sync.md) before landing your stack.
