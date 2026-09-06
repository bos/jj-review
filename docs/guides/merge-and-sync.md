---
title: Merge and sync
linkTitle: Merge and sync
description: Merge your ready pull requests and finish merges that happened on GitHub.
navGroup: Everyday work
weight: 50
---

Use `jj-stack merge` to merge submitted pull requests, starting at the bottom of your stack.
After a merge, `jj-stack sync` updates your local stack and the pull requests that remain open.
For a direct merge, which GitHub performs immediately rather than through a merge queue,
`jj-stack merge` runs that sync for you.

## Before merging

If you rewrote one of your changes after submitting it, submit your stack again, even if that
change's diff is unchanged:

```console
jj-stack submit <head-change-id>
jj-stack merge <head-change-id>
```

`jj-stack merge` checks that the changes to merge still match the commits you last submitted and
that their PR branches and pull requests have not moved unexpectedly. GitHub decides whether
checks, approvals, conflicts, and repo rules allow the merge.

## Choose how much of your stack to merge

By default, `jj-stack merge` selects consecutive submitted, open, non-draft pull requests from
the bottom of your stack. A draft, closed PR, or change that no longer matches its submitted
commit stops the selection; pull requests above it are left for later.

To merge only the bottom portion of your stack, use `--pull-request` to name the last PR to merge.
For example, suppose PR 42 depends on PR 41, and PR 43 depends on PR 42:

```console
jj-stack merge --pull-request 42
```

This asks GitHub to merge PRs 41 and 42. PR 43 stays open. After a direct merge, the automatic
sync rebases its local change and updates its PR branch and base to follow the new trunk.

If GitHub allows several merge methods and you have not configured a preference, specify one
with `--method`, for example `jj-stack merge --method squash <head-change-id>`. A merge queue
uses the method configured on GitHub.

## Finish after GitHub merges

Run `jj-stack sync` after a merge queue finishes or after someone merges your pull requests
through GitHub, `gh`, or another client. It fetches trunk, removes obsolete local copies of the
merged changes, rebases the remaining changes, updates their existing pull requests, and removes
PR branches that are no longer needed.

Select the stack by its head change ID or by any linked pull request:

```console
jj-stack sync <head-change-id>
jj-stack sync --pull-request <pr>
```

`jj-stack sync --pull-request` updates the complete local stack containing that PR, including
changes above it. The selected PR can already be merged if jj-stack still has its saved link.

If none of the pull requests has merged and GitHub has not rebased the stack, `jj-stack sync`
reports that there are no merged changes. Use `jj-stack submit` to publish local edits.

For example, after a squash merge, trunk contains a new commit while your original changes may
still appear in local history. `jj-stack sync` removes those obsolete copies. If you edited a
merged change after submitting it, the command stops so that it does not discard your edits.

### Merge queues

When `jj-stack merge` uses a merge queue, success means GitHub accepted the pull requests into the
queue. Wait until GitHub reports that they have merged, then run `jj-stack sync` for that stack.

While any selected pull request is queued, `jj-stack submit` refuses to update the stack and
`jj-stack sync` leaves it unchanged.

### Rebasing from GitHub

After GitHub's **Rebase stack** action finishes, run `jj-stack sync <head-change-id>` to bring
that rebase into your local stack.

GitHub's rewritten commits do not retain jj change IDs. `jj-stack sync` checks that the PR order
and contents match, rebases your original changes, and updates the PR branches with commits that
retain their change IDs. It stops if local edits or different contents on GitHub prevent a match.

### Several merged stacks

To sync every local stack affected by a completed merge, run:

```console
jj-stack sync --all
```

This also cleans up merged PRs whose local changes are gone. If one stack cannot be updated,
jj-stack explains why and continues with independent stacks.

`jj-stack sync --all` handles completed merges only. After GitHub's **Rebase stack** action,
select that stack explicitly with `jj-stack sync <head-change-id>`.

## If `merge` fails after GitHub merges your pull requests

A network failure or interrupted local update can leave the merge complete on GitHub but the
sync unfinished. Follow the recovery command in the error, normally:

```console
jj-stack view <head-change-id>
jj-stack sync <head-change-id>
```

Your pull requests are already merged, so do not retry `jj-stack merge`. `jj-stack sync` checks
the current local and GitHub state and finishes the remaining work.

## When trunk moves without one of your pull requests merging

`jj-stack sync` does not rebase a stack merely because trunk advanced. Fetch and rebase with `jj`
when you need the latest trunk, then submit the rewritten changes:

```console
jj git fetch
jj rebase -s '<bottom-change-id>' -o 'trunk()'
jj-stack submit <head-change-id>
```

Rebase when your work needs the latest trunk or GitHub requires it before merging.
