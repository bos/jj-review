---
title: Separate a stack or close pull requests
linkTitle: Separate or close
description: >-
  Break up your GitHub stack without closing your pull requests, or close your work without
  merging it.
navGroup: Everyday work
weight: 75
---

You can remove a GitHub stack's grouping while keeping its pull requests open, or close the PRs
when you no longer plan to merge them:

- `jj-stack unstack` removes the grouping.
- `jj-stack cleanup --pull-request <pr> --close` closes one PR and removes its unused branch,
  stack overview comment, and saved link.

Both commands keep your local changes. Use `jj abandon` separately if you also want to discard
them locally.

## Remove a stack's GitHub grouping

To stop grouping the PRs as a GitHub stack, run:

```console
jj-stack unstack <head-change-id>
```

The PRs remain open, with the same base branches and dependencies. Removing the grouping does
not make dependent PRs independently mergeable. You can review, update, or close them
individually.

The local stack also stays intact. Submitting it again recreates the GitHub grouping; to split it
into separate stacks, first [rearrange your changes with `jj`](multiple-stacks.md).

If jj-stack says the GitHub grouping no longer matches your local stack, use the
`--stack <number>` command printed in the error.

## Close the PRs in your stack without merging them

First run `jj-stack unstack <head-change-id>` if the PRs belong to a GitHub stack. Then close and
clean up each PR, starting at the top of the stack and working downward:

```console
jj-stack cleanup --pull-request <pr> --close
```

The command changes the PR's base to trunk before closing it, then removes its unused PR branch,
stack overview comment, and saved link. Working from the top down frees each lower PR's branch
before you try to remove it. Review comments and revision-history comments remain on GitHub.

If you already closed the PRs through GitHub or `gh pr close`, run:

```console
jj-stack cleanup <head-change-id>
```

### If cleanup keeps a branch

Cleanup keeps a PR branch while another open or reopenable closed PR uses it as its base. The
message names the dependent PR. Retarget an open PR to trunk. For a closed PR, either reopen and
retarget it or delete its head branch if you no longer need to reopen it. Then rerun cleanup.

A branch also stays while an unmerged PR in a GitHub stack needs it. Remove that grouping with
`jj-stack unstack` before retrying cleanup.

## Close an orphaned pull request

If you abandoned a submitted change with `jj abandon`, `jj-stack list` shows its PR as an
**orphan**: a saved pull request link whose local change is gone. Select that PR directly to
close and clean it up:

```console
jj-stack cleanup --pull-request <pr> --close
```

If the PR is still part of a GitHub stack, first remove the grouping with the
`jj-stack unstack --stack <number>` command printed in the error. The same branch-retention
rules apply to orphaned PRs.
