# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

Atomic state file writes are in place. The remaining gaps, in dependency order:

### 1. Exclusive state lock (prerequisite for everything below)

An exclusive advisory lock (`fcntl` on a `lock` file in the state directory)
must be acquired before any mutating command reads or writes state. Without it,
a concurrent process can observe a live intent file as if it were interrupted,
and two writers can corrupt the cache or intent directory. Read-only `status`
(no `--fetch`) does not need the lock. Locking is a prerequisite for intent
files, not a follow-up.

When the state directory is unavailable (no writable path, `jj config path
--repo` fails), mutating commands must hard-fail rather than proceeding
silently. The current code allows mutations to continue with persistence
disabled; that is safe today only because there is no intent file or lock to
depend on. Once this work lands, commands that cannot write state must refuse to
run.

### 2. Incremental cache saves

The cache is currently written once at the end of a successful run. A crash
mid-stack leaves the cache stale, so `status` shows no PR linkage for completed
revisions. Save the cache after each per-revision sync in both `submit` and
`cleanup --apply` (which also performs multiple remote mutations before its
single end-of-run save). This makes the cache accurate at any crash point.

### 3. Intent files

Write a per-operation intent file (atomically, under the state lock) before any
mutations begin. Delete it (atomically) after all mutations and the final cache
write complete. If the file exists when a command starts, the previous operation
was interrupted.

**File naming**: `incomplete-YYYY-MM-DD-HH-MM.NN.toml` (e.g.
`incomplete-2026-03-18-14-40.01.toml`). `NN` starts at `01` and increments if
a file with that timestamp already exists. Names sort naturally by creation
time. Colons are avoided because they are problematic on macOS and Windows. The
operation kind and all other fields are read from file contents; scanning all
`incomplete-*.toml` files is cheap since the typical count is zero.

**Per-kind payloads and semantics**: `submit` and `cleanup` have different
shapes and different matching needs; do not fold them together.

- `submit`: stores the **ordered** list of change IDs (bottom to top), the
  display revset string, a user-facing label (e.g. `"submit on @"`), and the
  start timestamp. Order matters because each PR base is derived from the
  previous revision in the chain; a reordered or reparented stack with the same
  change IDs is not the same operation.

- `cleanup-apply`: repo-wide; no change IDs. Stores the display revset and
  start timestamp.

- `cleanup-restack`: stores the ordered change IDs for the selected path, the
  display revset, and the start timestamp (same topology-aware semantics as
  `submit`).

**Matching for `submit` and `cleanup-restack`**: when a new operation starts,
scan all outstanding intent files of the same kind and compare ordered change ID
lists:

- **Exact match** (same IDs, same order): "resuming interrupted submit on `@`"
- **New is a strict ordered superset** (same prefix, new adds trailing
  revisions): proceed normally; clean up the old intent on success. This is the
  common case — extend the stack and re-submit after an interrupt.
- **Partial or reordered overlap**: warn that this operation overlaps an
  incomplete earlier one; proceed.
- **Disjoint**: brief notice only, do not block.

**UX contract**:

- `status` always lists outstanding intent files prominently and exits non-zero
  when any are present, consistent with how it treats other incomplete
  inspection.
- Each intent file includes a short user-facing label stored at write time
  (e.g. `"submit on @"`, `"cleanup --apply"`) so `status` can display it
  without re-evaluating revsets.
- An intent file is considered stale if none of its change IDs resolve to any
  revision in the local repo. Stale intents are reported separately and should
  be retirable via `cleanup` or an explicit flag.

## Aborting Incomplete Operations

Once intent files and incremental cache saves are in place, `submit --abort`
and `cleanup --abort` become well-defined: use the intent file to identify what
was in progress and the cache to identify what completed, then retract the
completed work (close PRs, delete remote branches, revert local bookmarks,
clear cache entries) and remove the intent file. Design separately; don't tie
to the intent file implementation.

## Concurrent Operations

Running two submits simultaneously (two terminals, a script, an accidental
double-enter) or two cleanups at once is currently unsafe: both processes read
the same cache, perform overlapping mutations, and write back independently,
with the last writer winning. This is addressed by the exclusive state lock
described under Crash and Interrupt Recovery above. Tracked here as a
reminder that the lock design must cover concurrent access explicitly, not
just crash recovery.

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
