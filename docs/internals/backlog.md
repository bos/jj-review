# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

_Benefit: medium — affects users with interrupted operations, which is uncommon
but leaves them stuck with inconsistent state until resolved._

Intent files now act as the concurrency lock, mutating commands hard-fail when
saved jj-review data is unavailable, saved-data writes are incremental during
mutating operations, `status` surfaces outstanding and stale incomplete
operations, and `abort` retracts completed work from an interrupted submit and
removes the intent file.

The remaining follow-up in this area is extending abort to cover partial land
retraction and `close` reversal (reopening closed PRs), both of which require
GitHub access and careful ordering of retraction steps.

## Ancestor Merged on GitHub

_Benefit: small — remaining edge cases are narrow and infrequent._

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

## Repo-Scoped Sync

_Benefit: medium — useful for operators managing several stacks at once, but
not blocking the core single-stack workflow._

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

_Benefit: medium — high value for teams that require merge queues, but complex
to design correctly and not blocking the current direct-push flow._

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

## Guided Recovery and Next-Step UX

_Benefit: large — daily operator quality of life; makes the safe next action
obvious without requiring users to read internal design notes._

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

## Documentation

_Benefit: large — Phases 2–4 increase adoption and reduce confusion;
without complete task-oriented guides, all other features are underutilized._

Phase 1 is complete: the README has a quickstart, and `docs/` has
`daily-workflow.md`, `mental-model.md`, and `troubleshooting.md`. Internal
design and implementation notes live under `docs/internals/`.

Remaining work:

- **Phase 2 (partial):** `mental-model.md` exists, but there is no standalone
  landing/cleanup guide, no importing-existing-PRs guide, and no cheatsheet
  for operators who already know the model.
- **Phase 3:** generated or semi-generated command reference pages that stay
  in sync with the argparse surface; doc drift checks that fail CI when
  committed reference pages diverge from actual `--help` output; example
  transcripts captured from the fake GitHub test environment.
- **Phase 4:** LLM-friendly exports (`llms.txt` / `llms-full.txt`) once the
  primary docs structure is stable.

Docs should teach the workflow first and enumerate commands second. The primary
risk is writing reference prose before the task-oriented guides are complete.
