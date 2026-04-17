# Troubleshooting

This page is organized by symptom and next command.

## `status` or `submit` says the stack selection is ambiguous

Cause:

- the current repo state doesn't resolve to one clear stack
- the remote or trunk branch is configured in an unusual way
- the revset you passed doesn't point at what you expected

What to do:

```bash
jj-review status
```

If needed, pass an explicit revset:

```bash
jj-review status <revset>
jj-review submit <revset>
```

The tool stops and reports what is ambiguous rather than guessing.

## GitHub shows different PR state than `status` reports

Cause:

- remembered remote bookmark state is stale
- a PR link or review branch changed on another machine or workspace
- you want to refresh both live GitHub state and local remote-bookmark observations

What to do:

```bash
jj-review status --fetch
```

`status` already checks live GitHub state when GitHub is reachable. `status
--fetch` also refreshes remembered remote bookmark state before reporting, so
it is the safer read-only refresh when a PR link, branch state, or merged-base
relationship may have changed elsewhere.

## Part of your stack landed and the rest needs to be rebased

Cause:

- some changes at the bottom of your stack landed
- the remaining changes are still based on the old history

What to do:

```bash
jj-review cleanup --restack
jj-review submit
```

`cleanup --restack` rebases your remaining changes above the newly landed
commits. After that, `submit` refreshes the open PRs to reflect the new base.

## PRs for this stack exist on GitHub but `jj-review` doesn't know about them

Cause:

- the stack was submitted from a different machine or workspace
- you cloned the repo and want to pick up review work that is already in progress

What to do:

```bash
jj-review import --pull-request <number-or-url> --fetch
```

Use `import` when the problem is "these PRs exist on GitHub but I can't manage
them locally yet." It is not for rewriting history or changing what is in the
stack — only for telling `jj-review` which local changes go with which PRs.

## Old review branches are still around after landing or closing

Cause:

- the land or close succeeded, but the follow-up cleanup hasn't run yet
- you ran `land --skip-cleanup` to keep the review branches on purpose
- something prevented `jj-review` from cleaning up automatically
- an older `jj-review` version left local `review/*` bookmarks behind on
  already-landed or otherwise inactive history

What to do:

```bash
jj-review cleanup --dry-run # optional
jj-review cleanup
```

Use `--dry-run` if you want first, to preview what it plans to remove. Then run plain `cleanup`
to apply the safe stale-state cleanup it described.

## You want to stop reviewing a stack on GitHub

Cause:

- the work was abandoned, replaced, or is no longer meant for review

What to do:

```bash
jj-review close
```

If you already know the pull request number, you can use:

```bash
jj-review close --pull-request 7
```

This closes the selected stack's pull requests. Add `--cleanup` if you also
want to delete the review branches and clean up local tracking data for that
stack.

## A command was interrupted before it finished

Cause:

- `submit` or another mutating command was cut short (Ctrl-C, crash, network
  failure) after it had already done some work but before it finished
- `status` reports an interrupted operation

What to do:

```bash
jj-review status
```

Check what `status` says is incomplete. Then preview what `abort` would undo:

```bash
jj-review abort --dry-run
```

If the plan looks right, apply it:

```bash
jj-review abort
```

Use `abort` when you want to retract an interrupted `submit`.

Otherwise, follow the command that `status` tells you to rerun:

- re-run `submit <revset>` to finish or refresh the stack you explicitly
  select on GitHub
- re-run `close` or `close --cleanup` if `status` names one of those
- re-run `cleanup --restack` to finish restacking the current stack
- re-run `land` to finish landing; `abort` cannot un-merge changes that already
  reached trunk

For interrupted commands other than `submit`, `abort` clears the
interrupted-operation record. It does not automatically reverse a completed
land, restore the old local history after a restack, or reopen pull requests.

### `abort` refuses because the stack has changed

If you rewrite or reorder the stack after a `submit` was interrupted, `abort`
will not try to guess which PRs or review branches came from that interrupted
submit.
In that case you have two options:

- **Finish the submit**: re-run `submit <change-id-from-status>` or another
  explicit revset for the stack you want. It detects any review branches or PRs
  that already exist, and completes whatever is still outstanding for that
  selected stack.
- **Retract the partial work**: run
  `jj-review close --cleanup <change-id-from-status>` or another explicit
  revset for that stack. A successful `close --cleanup` closes the open PRs,
  deletes the review branches, and clears the interrupted `submit` record once
  the recorded review artifacts for that stack are gone.
