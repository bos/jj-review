# Troubleshooting

This page is organized by symptom and next command.

## `status` or `submit` says the stack selection is ambiguous

Cause:

- the current repo state does not resolve to one clear selected stack
- the remote/trunk mapping is ambiguous
- the selected revision is not the one you thought it was

What to do:

```bash
jj-review status
```

If needed, rerun the command with an explicit revset:

```bash
jj-review status <revset>
jj-review submit <revset>
```

The tool is expected to fail closed here. It should not guess.

## GitHub state looks stale

Cause:

- local knowledge of remote bookmarks is old
- GitHub pull request state has changed since the last inspection

What to do:

```bash
jj-review status --fetch
```

Use that before attempting repair if the main uncertainty is whether remote
state has changed.

## Earlier work landed and later work now needs local repair

Cause:

- the ready prefix landed
- remaining local changes still sit above the old ancestry shape

What to do:

```bash
jj-review cleanup --restack
jj-review submit
```

`cleanup --restack` is the explicit local-history repair path after landing.

## A PR stack exists on GitHub but local tracking is missing

Cause:

- local saved jj-review state was never created here
- the stack was created elsewhere and needs to be materialized locally

What to do:

```bash
jj-review import --pull-request <number-or-url> --fetch
```

Use `import` when the problem is "this stack exists remotely, but `jj-review`
does not yet know which local changes go with those review branches and PRs,"
not when the problem is "my local stack needs to be rewritten."

## A review branch or saved state looks stale after closing or landing

Cause:

- the stack was closed or landed, but later cleanup has not run yet
- `land --skip-cleanup` kept the landed local review bookmarks on purpose
- saved state or remote review branches remain because `jj-review` could not
  prove it was safe to delete them automatically

What to do:

```bash
jj-review cleanup
```

Inspect first. If the command reports a safe restack or cleanup action, follow
the printed guidance.

## The current stack should stop being managed on GitHub

Cause:

- the work was abandoned, replaced, or intentionally removed from review

What to do:

```bash
jj-review close
```

Add `--cleanup` only when you also want it to delete review branches and prune
saved state.

## You only need exact flags and parser behavior

Use the built-in help:

```bash
jj-review --help
jj-review help --all
jj-review <command> --help
```
