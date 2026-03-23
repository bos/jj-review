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
batch or parallelize GitHub API calls. Acceptable to stay serial for now.

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

The design doc and future `land` design now cover the main recovery shape for
merged ancestors and the division of labor between `land` and
`cleanup --restack`.

The remaining follow-up here is narrower:

- edge cases around partial-stack landing boundaries after an earlier prefix
  has already landed
- whether future landing transports impose extra constraints on how descendants
  are rediscovered and resubmitted
- any residual diagnostics that are still too subtle once the concrete `land`
  flow exists

## Bookmark Naming Collisions

The current design rejects bookmark naming collisions from user overrides, but
two changes could theoretically produce the same slug+suffix. The 8-char
`change_id` suffix makes this extremely unlikely, but the tool should detect
it and fail with a clear diagnostic describing what went wrong and how to
resolve it (e.g., set an explicit bookmark override for one of the changes).

## Draft PR Support

`submit` now supports creating new draft PRs and publishing existing draft PRs
on the selected path.

The remaining follow-up is narrower:

- decide whether the product also needs an explicit standalone command to mark
  existing PRs ready or convert them back to draft outside `submit`
- decide whether future metadata/config support should let some changes default
  to draft creation without requiring `submit --draft`

## Re-Request Review

A future `submit --re-request-review` style option may be worthwhile for the
"addressed feedback, please look again" workflow.

The feature concept is straightforward, but the source of truth for "who
should be re-requested" is not. Possible inputs include the current requested
reviewer list on GitHub, prior review authors, or tool-configured reviewers,
and each choice has surprising edge cases once reviewers are added, removed,
or changed outside `jj-review`.

Design separately before implementing so the eventual UX is explicit about
which reviewer set it uses and when notifications are sent.

## Repo-Scoped Sync

A future `import` design covers explicit stack materialization for one
selected review stack, and `status --fetch` remains the read-only refresh
primitive.

The remaining open question is whether the product should also grow a
repo-scoped `sync` command that:

- refreshes remote review observations across more than one selected stack
- decides when local bookmark materialization should happen automatically
- coordinates with `cleanup --restack` without turning refresh into implicit
  history repair
