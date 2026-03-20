# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

Intent files now act as the concurrency lock, mutating commands hard-fail when
review state is unavailable, cache writes are incremental during mutating
operations, and `status` surfaces outstanding and stale incomplete operations.

The remaining follow-up in this area is explicit abort support.

## Aborting Incomplete Operations

Once intent files and incremental cache saves are in place, `submit --abort`
and `cleanup --abort` become well-defined: use the intent file to identify what
was in progress and the cache to identify what completed, then retract the
completed work (close PRs, delete remote branches, revert local bookmarks,
clear cache entries) and remove the intent file. Design separately; don't tie
to the intent file implementation.

## Concurrency and Rate Limiting

The submit algorithm walks bottom-to-top creating/updating PRs sequentially.
For deep stacks this means many API round trips. We need to decide whether to
batch or parallelize GitHub API calls. Acceptable to stay serial for the MVP.

The GitHub client already implements retry-with-backoff for 429 and 403
rate-limit responses, reading `Retry-After` and `X-RateLimit-Reset` headers
and falling back to exponential backoff. The remaining gap is parallelising
the per-change API calls in `submit` and `status` for large stacks.

Now that `submit` is moving toward phase-based batching and bounded
concurrency, the CLI progress model should be revisited separately from the
throughput work. In particular, a TTY-only spinner or live per-change progress
view would likely fit the new batched execution model better than the older
line-open incremental renderer. Design that as an explicit UX follow-up rather
than coupling it to the remote/GitHub concurrency changes.

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
