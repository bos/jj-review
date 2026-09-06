---
title: Edit and rearrange a stack
linkTitle: Edit and rearrange
description: >-
  Change your submitted stack with ordinary jj commands while keeping the same pull requests.
navGroup: Everyday work
weight: 40
---

Edit your stack with ordinary `jj` commands, then run `jj-stack submit` to update GitHub. Pull
requests follow change IDs, so rewriting or moving a change keeps its existing PR and discussion.

## Edit a change in your stack

Edit the change you want, then resubmit your stack:

```console
jj edit <change-id>
# make the requested changes
jj-stack submit <head-change-id>
```

`jj` rebases descendants when you edit a change. Resolve any conflicts before submitting, and
select the stack's head so the PRs above the edited change are updated too.

## Reorder changes

Use `jj arrange` or `jj rebase` to change the order. Check the resulting stack before submitting:

```console
jj arrange
jj-stack view <head-change-id>
jj-stack submit <head-change-id>
```

Use the head change ID from the new order. `jj-stack submit` updates the PR order and base
branches to match. If you are moving changes between stacks, follow
[multiple stacks](multiple-stacks.md#move-work-between-stacks) for the submission order.

## Split or squash

When you split a change, the part that keeps the original change ID also keeps its pull request.
The new change gets a new PR on the next submit.

When you squash changes, each surviving change keeps its PR. Submit the resulting stack, then
close the PRs for any removed changes using the cleanup command below.

## Abandon one of your submitted changes

`jj abandon` removes the local change and leaves its PR open. If other changes remain in the
stack, submit them first so their PRs no longer depend on the removed change. Then close the
orphaned PR and remove its unused branch and saved link:

```console
jj-stack cleanup --pull-request <pr> --close
```

For a whole stack, follow [Separate a stack or close pull requests](close-or-separate.md).
