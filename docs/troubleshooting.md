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

- remote bookmark state is stale
- a PR was approved, merged, or closed since your last fetch

What to do:

```bash
jj-review status --fetch
```

This fetches the latest state from GitHub before reporting. Use it any time
you want to make sure you're working with current information before acting.

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

What to do:

```bash
jj-review cleanup
```

Run without flags first to see what it plans to remove. It will describe
what it found and what it will do.

## You want to stop reviewing a stack on GitHub

Cause:

- the work was abandoned, replaced, or is no longer meant for review

What to do:

```bash
jj-review close
```

This closes the pull requests. Add `--cleanup` if you also want to delete the
review branches and clean up local tracking data for the stack.

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

Once the plan looks right, apply it:

```bash
jj-review abort
```

After aborting, the stack is back to a clean state and you can re-run the
original command from scratch.

If the interruption happened late enough that all the work actually went
through, re-running the original command is safer than aborting — `submit`
in particular will use your current selected stack rather than trying to replay
an old `@` or `@-` snapshot, while still keeping enough recovery data for
`abort`.

`close` follows the same current-stack-first rule. If you changed the stack
before re-running `close`, it will act on the current selected stack rather
than trying to replay the old selector. `close --cleanup` is treated as a
stronger follow-up than plain `close`: it can cover an older interrupted plain
`close`, but a later plain `close` does not erase an older interrupted cleanup
run. If `status` shows an interrupted close, rerun whichever close command it
names (`close` or `close --cleanup`) if you want to finish that operation.

`cleanup --restack` also works from the current selected stack on rerun. If
the stack was rewritten after the interruption, jj-review reports that it is
using the current stack rather than pretending it is replaying the original
selector.

If you rewrote or reordered the stack after the interruption, `abort` will
refuse to guess which PRs or review branches belong to the old partial submit.
In that case, inspect the current stack with `status` and clean up manually if
you still need to unwind the old partial work.

## You only need the exact flags and options for a command

Use the built-in help:

```bash
jj-review --help
jj-review help --all
jj-review <command> --help
```
