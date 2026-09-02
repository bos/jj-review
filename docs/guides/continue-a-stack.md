---
title: Continue an existing stack
linkTitle: Continue an existing stack
description: Connect this checkout to your existing stack of pull requests on GitHub.
navGroup: Everyday work
weight: 70
---

Use this workflow when you submitted your stack from another machine or checkout and want to work
on it here.

## Pick a stack

List the active stacks already tracked here and those available only on GitHub:

```console
jj-stack checkout --pick
```

Each GitHub row shows the stack number, top pull request, base branch, size, status, and whether
the stack is local, partly local, or available only on GitHub. Choosing a partly local or
GitHub-only stack completes its local tracking, fetches any missing commits, and edits its top
active change. Choosing a local stack just edits its head.

## Connect a pull request directly

Choose any pull request in your stack:

```console
jj-stack checkout --pull-request <pr>
```

`checkout` follows that pull request down to the bottom of its stack, fetches those commits,
records which local change belongs to each pull request, and runs `jj edit` on its change.

To start a new change on top instead of editing that change directly, run:

```console
jj new
```

## If your change is already here with local edits

If this repo already has a different commit for the same change, usually because you edited it
after it was last submitted, `checkout` brings in the pull request's commit as a second copy of
that change and prints both commit IDs. It does not choose between them. Compare the two, then
abandon the one you do not want by its commit ID:

```console
jj log -r 'change_id(<change-id>)'
jj diff -r <commit-id>
jj abandon <unwanted-commit-id>
```

If you kept your own copy, update the pull request:

```console
jj-stack submit <head-change-id>
```

## If someone pushed a commit to your PR branch

A commit that someone else pushed to your PR branch, such as a reviewer's suggestion, is not part
of your change. `checkout` brings it in as a new change on top of yours and prints the
`jj squash` command that folds it into your change.
