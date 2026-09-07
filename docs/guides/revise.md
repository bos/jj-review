---
title: Edit and rearrange a stack
linkTitle: Edit and rearrange
description: >-
  Change your submitted stack with ordinary jj commands while keeping the same pull requests.
navGroup: Everyday work
weight: 40
---

Edit your local history with `jj`, then run `jj-stack submit` to update the corresponding
pull requests on GitHub.

## Edit a change in your stack

Find your stack's head change ID with `jj-stack list` or `jj log`. Edit the change you want,
then resubmit using that head:

```console
jj edit <change-id>
# make the requested changes
jj-stack submit <head-change-id>
```

`jj edit` takes the change you want to edit. `jj-stack submit` takes the top of the stack you
want to publish. For a stack A → B → C, use B's ID to edit B and C's ID to submit the whole stack.

When you edit B, `jj` automatically rebases C onto it. Both changes keep their existing pull
requests and discussions. C's PR branch also needs updating because the rebase changes its
commit ID, even if you haven't edited C's contents.

## Reorder changes

Rearrange your work with `jj`, then inspect and submit the result. Since a different change
may now be at the top, use `jj log` to find the new head before submitting:

```console
jj arrange
jj log
jj-stack view <head-change-id>
jj-stack submit <head-change-id>
```

Use the head change ID from the new order. `jj-stack submit` updates the PR order and base
branches to match. If you are moving changes between stacks, follow
[multiple stacks](multiple-stacks.md#move-work-between-stacks) for the submission order.

## Split or squash

When you split a change, the part that keeps the original change ID also keeps its pull request.
The new change gets a new PR on the next submit.

When you squash your changes, whichever change survives keeps its pull request. PRs for the
other changes remain open until you close and clean them up, as described below. `jj-stack`
never reuses those PRs for different work.

## Abandon one of your submitted changes

After `jj abandon` removes a change from your local history, its pull request and PR branch
remain on GitHub. To close the PR and remove its branch, run:

```console
jj-stack cleanup --pull-request <pr> --close
```

For a whole stack, follow [Separate a stack or close pull requests](close-or-separate.md).
