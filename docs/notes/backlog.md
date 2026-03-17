# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

There's a variety of ways in which a crash or ctrl-C can cause potentially
persistent failures. We need to figure out how to test for those, what the
failure modes are, and how to recover from those that we can't prevent.

We should also be mindful of atomic write-tempfile-then-rename modifications to
tool-controlled files, so that partially written files can't exist or be read.

## Concurrency and Rate Limiting

The submit algorithm walks bottom-to-top creating/updating PRs sequentially.
For deep stacks this means many API round trips. We need to decide whether to
batch or parallelize GitHub API calls. Acceptable to stay serial for the MVP.

The GitHub client already implements retry-with-backoff for 429 and 403
rate-limit responses, reading `Retry-After` and `X-RateLimit-Reset` headers
and falling back to exponential backoff. The remaining gap is parallelising
the per-change API calls in `submit` and `status` for large stacks.

## Ancestor Merged on GitHub

The design doc says "require a local `jj rebase` before changing the PR base"
when an ancestor PR has merged. We need to flesh out:

- how the tool detects the mismatch between local parentage and GitHub merge
  state
- what the diagnostic looks like
- whether there are edge cases around partial-stack merges

## Bookmark Naming Collisions

The MVP rejects bookmark naming collisions from user overrides, but two changes
could theoretically produce the same slug+suffix. The 8-char `change_id` suffix
makes this extremely unlikely, but the tool should detect it and fail with a
clear diagnostic describing what went wrong and how to resolve it (e.g., set an
explicit bookmark override for one of the changes).

## Draft PR Support

GitHub has a native draft PR concept (visible but not reviewable or mergeable
until marked ready). We should eventually support creating PRs as drafts and
promoting them, but the semantics need to be designed before adding it. Deferred
from MVP.

## Private Commits

`jj` can be configured with `git.private-commits` to refuse pushes for commits
matching a revset, and for descendants that would require pushing those commits
too. `submit` should preflight that policy and fail with a targeted diagnostic
before attempting `jj git push`.

## Submit Dry-Run Mode

`submit` currently mutates local bookmarks, pushes branches, and
creates/updates PRs in a single command with no preview step. `status` serves
as the pre-flight inspection, but it does not show what submit would actually
do (which bookmarks would move, which PRs would be created vs. updated, what
the computed base branches would be).

A `submit --dry-run` flag that prints the planned bookmark moves, pushes, and
PR actions without performing them would lower the friction for first-time
submits and make it easier to verify that a rebase or rename has been
interpreted correctly before touching GitHub. The planned output format should
match what `submit` prints on a live run so the user knows exactly what to
expect.

## Status Command Architecture

`status` now prepares local state first, prints the local header immediately,
and streams per-change rows after bounded concurrent GitHub inspection starts.
It still keeps a collected `StatusResult` as a secondary API for tests and any
future non-streaming callers.

We should still revisit whether `status` should:

- show explicit in-progress markers while GitHub inspection is underway
- keep a top-level collected `StatusResult` object at all, or switch fully to
  streamed status events
- separate repo-level GitHub reachability from per-change review state even
  more cleanly in the renderer
