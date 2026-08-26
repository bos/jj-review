---
title: Work with a stack on GitHub
linkTitle: Work on GitHub
description: Know which GitHub edits jj-stack preserves, replaces, or rejects.
navGroup: Everyday work
weight: 45
---

GitHub remains the place to review, discuss, check, and merge a stack. Your local `jj` history
remains the source of truth for its changes and order.

## Safe GitHub actions

You can comment, review, approve, request changes, add labels, request reviewers, and inspect or
rerun checks normally. You can also merge through GitHub's native stack UI or use **Rebase
stack**. After a merge or rebase finishes, run:

```console
jj-stack sync <head-change-id>
```

Do not rewrite the same stack locally while a GitHub rebase or merge is in progress. If local
edits and GitHub's rewritten contents disagree, `sync` stops instead of choosing one.

## What the next submit replaces

`jj-stack submit` makes GitHub's stack match your local stack. It pushes each change in your stack
to its PR branch, sets each pull request's base from the local parent order, and updates native
stack membership.

By default, `submit` will generate a pull request description from the `jj` change. On a
subsequent submit, if the PR description still matches the last automated PR description,
`submit` will refresh it from the current change description. Otherwise, `submit` will leave it
untouched.

Use `--describe` to replace one body deliberately (or `--describe-with` or `--edit` for titles and
bodies).

`--draft` affects new pull requests. `--draft=all`, `--open`, and the choices made through
`--edit` can change existing draft states. Labels and reviewer requests that submit applies are
additive; unrelated existing labels and reviewers are not removed.

## Changes to avoid on GitHub

Do not force-push, rename, or delete `jj-stack/` PR branches. If a branch moves unexpectedly,
jj-stack stops instead of overwriting it. See [troubleshooting](../troubleshooting.md) for the
recovery steps.

Do not change pull request bases or GitHub stack membership by hand. `jj-stack submit` derives
both from the local `jj` history. To keep a different base or order, make that change locally and
submit again. To leave the pull requests open but remove their GitHub stack grouping, run
`jj-stack unstack --stack <number>`.

For reviewer and repo configuration guidance, see
[review and operate a stack](review-a-stack.md).
