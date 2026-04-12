# Daily workflow

This is the normal author loop `jj-review` is designed around.

## 1. Build your local stack with `jj`

Create some local changes that you want reviewed. For example:

- refactor the shared model
- add the API
- add the UI

Keep your stack linear (or rewrite it to be linear prior to review). `jj-review` is
intentionally focused on one linear chain of reviewable changes at a time.

## 2. Inspect before submitting

`jj-review` will by default submit the stack of changed between `trunk()` and `@-` (the most
recent change below your working directory).

You can easily check what the tool thinks that stack is:

```bash
jj-review status
```

This is the safest first command whenever you are unsure what might be submitted.

You may also notice `review/...` bookmarks in `jj`. Those are the local review
branches `jj-review` uses for GitHub PR heads.

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
`jj-review submit` creates the matching `review/...` bookmark for it. After
that, it reuses that bookmark as the stable GitHub PR head branch while you
keep rewriting the local change.

## 4. Revise locally as reviews come in

Make the requested changes in `jj`. Split, squash, reorder, or rewrite locally
as needed.

Once the local stack looks right again, refresh GitHub:

```bash
jj-review submit
```

## 5. Check readiness

Use `status` when you need to answer:

- which changes already have PRs
- which PRs are draft, approved, blocked, or need cleanup

For more detail:

```bash
jj-review status --verbose
```

## 6. Land the changes that are ready

When the bottom part of the stack is ready to go:

```bash
jj-review land
```

(What does "ready to go" mean? State on GitHub is open, not draft, approved, and no outstanding
changes requested.)

If you want to inspect the landing plan first:

```bash
jj-review land --dry-run
```

By default, a successful `land` also forgets the local `review/...` bookmarks
for the changes that actually landed. Use `--skip-cleanup` if you want to keep
those local review bookmarks.

`land` works on consecutive changes above `trunk()`, not on arbitrary changes in the middle of
your stack. To land mid-stack changes, use e.g. `jj arrange` or `jj rebase` to reorder your
stack to move them to the bottom.

## 7. Restack remaining work

If later changes remain outstanding above work that just landed, you can quickly fix up your
local stack:

```bash
jj-review cleanup --restack
```

There might be open PRs for your remaining not-yet-landed changes on GitHub, which could now
point at old branch targets, old parent PRs, or old diffs. You can easily refresh GitHub's
state:

```bash
jj-review submit
```

## 8. Close abandoned review stacks

If a stack should no longer be reviewed:

```bash
jj-review close
```

Use `--cleanup` only when you also want it to delete review branches and prune
saved state after the PRs are closed.

## Short version

The steady-state loop is:

```bash
jj-review status
jj-review submit
# edit in jj
jj-review submit
jj-review land
jj-review cleanup --restack
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

See the [troubleshooting guide](troubleshooting.md) for more recovery scenarios.
