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
batch or parallelize GitHub API calls, and handle GitHub rate limiting
gracefully. Acceptable to stay serial for the MVP.

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

## Minimum JJ Version

The implementation shells out to `jj` and relies on machine-readable template
output. We need to either pin a minimum `jj` version or add a capability check
at startup to confirm the expected template syntax works.

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

## Status Command Architecture

The current `status` implementation computes a full `StatusResult` before
printing anything. That makes it easy to bundle repo-level and per-change
state, but it also means the command cannot stream output incrementally and may
be carrying a command-specific result object that does not justify its added
indirection.

We should revisit whether `status` should:

- print incremental progress or per-change output as it inspects GitHub state
- keep a top-level `StatusResult` object at all, or instead stream status
  events / render directly from the handler
- separate repo-level GitHub reachability from per-change review state more
  cleanly
