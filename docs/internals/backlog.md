# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

Intent files now act as the concurrency lock, mutating commands hard-fail when
saved jj-review data is unavailable, saved-data writes are incremental during
mutating operations, `status` surfaces outstanding and stale incomplete
operations, and `abort` retracts completed work from an interrupted submit and
removes the intent file.

The remaining follow-up in this area is extending abort to cover partial land
retraction and `close` reversal (reopening closed PRs), both of which require
GitHub access and careful ordering of retraction steps.

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

- edge cases around partial-stack landing boundaries after some earlier changes
  have already landed
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

## Landing Transports and Merge Queues

The current `land` model is intentionally narrow: resolve the ready prefix,
move local history first, then reconcile GitHub state around that result.

The remaining product question is whether landing should eventually support
more than one transport while keeping the `jj` DAG as the source of truth.
Concrete follow-up questions:

- whether `land` should grow an explicit transport selector such as direct
  push to trunk, open a landing PR, or submit the ready prefix to a merge
  queue
- how queue-backed landing should report queued, running, failed, and merged
  states in `status` without introducing forge-owned stack metadata as a
  competing source of truth
- how the queue or landing-PR path should preserve the current fail-closed
  behavior when the ready prefix changes locally while a queued landing is in
  flight
- whether queue-backed landing needs resumable intent state distinct from the
  current direct-landing intent model
- how repo policy requirements such as required checks, branch protection, and
  review-only `review/*` branches should be diagnosed before a landing attempt

This should be designed explicitly rather than bolted onto the current `land`
flow piecemeal.

## Setup Diagnostics and Repository Readiness

The tool currently derives a lot of state automatically and fails closed when
that derivation is ambiguous. That is the right steady-state behavior, but the
onboarding and support experience still needs a more explicit diagnostic path.

A future `doctor` or `setup-check` style command could answer:

- whether GitHub authentication is available and has the scopes the selected
  operations need
- which remote and trunk branch jj-review resolved, and why
- whether the selected repository policy matches the intended review model
  (linear history, non-mergeable review branches, etc.)
- whether local jj config, repo config, and saved jj-review state disagree in
  ways that will cause future submit or land failures
- whether stale workspaces, conflicted bookmarks, or ambiguous remote bookmark
  mappings need local repair before review operations proceed

The key requirement is that this stays diagnostic and explanatory. It should
not silently mutate repo state just to make warnings disappear.

## Guided Recovery and Next-Step UX

The command surface is intentionally small, but the operator experience still
depends heavily on knowing what to run next after a non-trivial state change.

Useful follow-up work here includes:

- richer "next command" guidance after `submit`, `land`, `close`, and
  `cleanup --restack`
- clearer distinction between "inspect only", "safe retry", and "history
  rewrite" recovery paths when something is stale or ambiguous
- an explicit guided-recovery flow for common cases such as "ancestor already
  landed", "remote branch disappeared", or "saved state no longer matches the
  selected stack"
- whether some of the current recovery-oriented guidance should eventually live
  behind a dedicated helper command rather than being repeated ad hoc in
  diagnostics

This is partly presentation, but it is also a real product capability: the
tool should make the safe next action obvious without requiring the operator to
read internal design notes.

## Documentation Plan

The product now has enough surface area that the README alone is no longer the
right home for onboarding, workflow guidance, troubleshooting, and command
reference. We need a deliberate user-facing documentation set.

### Documentation Goals

The documentation should:

- get a new user from install to first submitted stack quickly
- explain the `jj`/`jj-review` split of responsibility without teaching all of
  `jj`
- center the core workflow rather than the full command inventory
- make failure modes feel explicit and diagnosable rather than mysterious
- stay aligned with actual CLI help and implemented behavior

### Information Architecture

The target shape is:

- a short README that answers what the tool is, who it is for, how to install
  it, and how to complete a five-minute first run
- task-oriented docs under a user-facing docs tree, likely including:
  - quickstart
  - mental model
  - daily workflow
  - landing and cleanup
  - importing existing PRs
  - troubleshooting and recovery
  - configuration
  - cheatsheet / command map
- generated or semi-generated command reference pages that mirror the current
  CLI help surface instead of drifting into hand-maintained prose
- contributor-only notes kept separate from end-user documentation

### Content Priorities

The first pass should prioritize:

- one canonical "five minutes to first stack" guide
- one "daily workflow" guide that covers `status`, `submit`, `land`, and
  `cleanup --restack` together instead of as isolated commands
- a mental-model page explaining why mutable history stays in `jj` while
  GitHub review state lives in `jj-review`
- a troubleshooting page organized by symptom and next command, not by
  internal subsystem
- a concise cheatsheet for operators who already understand the model and only
  need command reminders

Later passes can add migration notes, richer examples, and policy/setup docs.

### Documentation Tooling

Docs should not become a second, stale command reference. Follow-up work:

- decide whether command reference pages are generated directly from argparse
  help text or from a small checked-in intermediate representation
- add doc checks that fail when generated help output diverges from committed
  reference pages
- prefer example transcripts captured from the fake GitHub test environment so
  command output examples remain realistic and reviewable
- keep user-facing terminology consistent with the CLI, especially around
  `change_id`, stack head selection, submit, import, and cleanup

### Documentation Delivery Phases

Reasonable phases:

- Phase 1: shorten the README, add quickstart, daily workflow, and
  troubleshooting pages
- Phase 2: add mental-model, landing/cleanup, and import guides, plus a
  cheatsheet
- Phase 3: add generated command reference and doc drift checks
- Phase 4: add LLM-friendly exports such as `llms.txt` / `llms-full.txt` once
  the primary docs structure is stable

The primary risk is writing too much reference prose before the task-oriented
guides exist. The docs should teach the workflow first and enumerate commands
second.
