# Daily workflow

This is the normal author loop `jj-review` is designed around.

## 1. Build your local stack with `jj`

Create some local changes that you want reviewed. For example:

- refactor the shared model
- add the API
- add the UI

Keep your stack linear (or rewrite it to be linear prior to review). `jj-review` is
intentionally focused on one linear stack at a time.

## 2. Inspect before submitting

`jj-review` will by default submit the current stack ending at `@-` (the most recent completed
change below your working directory). In the common case that is the stack you just built on
top of `trunk()`. If `trunk()` has advanced since you last rebased, your stack instead starts
from an older ancestor of `trunk()` — `jj-review status` shows that ancestor in the footer
beneath the stack, so you can see exactly what the stack is based on.

You can easily check what the tool thinks that stack is:

```bash
jj-review
```

This defaults to `jj-review status`.
`status` also accepts the short alias `st`.

This is the safest first command whenever you are unsure what might be submitted.

You may also notice review bookmarks in `jj`. By default they look like
`review/...`, but a repo can configure a different prefix. Those are the local
review branches `jj-review` uses for GitHub PR heads.

## 3. Submit the stack

Create or refresh the GitHub pull requests for the current stack:

```bash
jj-review submit
```

If you want to inspect the plan first:

```bash
jj-review submit --dry-run
```

If a change does not already have its review branch and PR set up,
`jj-review submit` creates the matching review bookmark for it. After that, it
reuses that bookmark as the stable GitHub PR head branch while you keep
rewriting the local change.

## 4. Revise locally as reviews come in

Make the requested changes in `jj`. Split, squash, reorder, or rewrite locally
as needed.

Once the local stack looks right again, refresh GitHub:

```bash
jj-review submit
```

If you want to ask prior reviewers to take another look after addressing
feedback, run:

```bash
jj-review submit --re-request
```

This will notify reviewers who approved or asked for changes to a PR.

## 5. Check readiness

Use `status` when you need to answer:

- which changes already have PRs
- which PRs are draft, approved, blocked, or need cleanup

If review state already exists on another machine or only on GitHub, run
`jj-review import` first to attach that stack locally.

For more detail:

```bash
jj-review status --verbose
```

Verbose mode keeps the native `jj log` line for each change, including any
bookmark names shown there.

## 6. Land the changes that are ready

When the bottom part of the stack is ready to go:

```bash
jj-review land
```

(What does "ready to go" mean? State on GitHub is open, not draft,
approved, and no outstanding changes requested, and the local change has
no unresolved conflicts.)

If you rebased a reviewed change without changing its diff, `land` refreshes
the review branch for you before it pushes `trunk()`. If you changed the diff
since the last review, rerun `submit` first so the PR shows the new content
and reviewers can take another look.

If you want to inspect the landing plan first:

```bash
jj-review land --dry-run
```

If you want to land only the ready prefix up through one specific pull request:

```bash
jj-review land --pull-request 7
```

By default, a successful `land` also forgets the local review bookmarks for
the changes that actually landed. Use `--skip-cleanup` if you want to keep
those local review bookmarks.

`land` works on the consecutive ready prefix of the selected stack, not on arbitrary changes in
the middle of your stack. To land mid-stack changes, use e.g. `jj arrange` or `jj rebase` to
reorder your stack and move them to the bottom first.

A normal successful `land` pushes those exact local commit IDs directly to
`trunk()`. If later local changes remain above the landed prefix, they usually
do not need rebasing just because lower changes landed.

## 7. Rebase remaining work

`jj-review cleanup --rebase` is specifically about removing merged ancestors from your local
stack and rebasing surviving descendants onto `trunk()`. Use it when some lower changes were
merged on GitHub through different commit IDs and your local stack still contains those
now-merged ancestors:

```bash
jj-review cleanup --rebase
```

`cleanup --rebase` does not otherwise rewrite history. If your stack simply drifted because
`trunk()` advanced without anything in your stack landing, rebase with plain `jj`:

```bash
jj rebase -s <bottom-of-stack> -d 'trunk()'
```

After `cleanup --rebase`, there might be open PRs for your remaining not-yet-landed changes on
GitHub that still point at old branch targets, old parent PRs, or old diffs. You can refresh
GitHub's state with:

```bash
jj-review submit
```

## 8. Close abandoned review stacks

If a stack should no longer be reviewed:

```bash
jj-review close
```

If you know the pull request number already, you can select the linked local
change directly:

```bash
jj-review close --pull-request 7
```

Use `--cleanup` only when you also want it to remove the stack's old review
branches and `jj-review`'s saved tracking data after the PRs are closed. If
`jj-review` created local review bookmarks for those branches, it forgets those
too. Reused user bookmarks stay unless `cleanup_user_bookmarks = true`.

## Short version

The steady-state loop is:

```bash
jj-review status
jj-review submit
# edit in jj
jj-review submit
jj-review land
jj-review cleanup --rebase
jj-review submit
```

## When something goes wrong

If a command is interrupted mid-way (crash, Ctrl-C, network failure), `status`
will report an outstanding incomplete operation. Use `abort` to retract the
partial work and get back to a clean state:

```bash
jj-review status        # see what is incomplete
jj-review abort --dry-run   # preview what would be retracted
jj-review abort         # retract and clean up
```

For interrupted `submit`, the recorded notice identifies the stack it started
from. Re-run `submit` or `close --cleanup` with an explicit revset for the
stack you want, not a naked command that falls back to the default selection.

If you rewrote that stack in the meantime, `abort` will not try to guess how to
undo the old partial submit.

See the [troubleshooting guide](troubleshooting.md) for more recovery scenarios.
