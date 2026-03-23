# JJ-Native Stacked GitHub Review Implementation Strategy

This document describes how we intend to implement the stacked GitHub review
tool described in [JJ-Native Stacked GitHub Review
Design](./design.md).

It is intentionally pragmatic. The goal is to describe how we will build the
tool, how we will test it, and how we will stage the work into reviewable
commits.

Anything marked `XXX` is not fully cooked yet and should be treated as a draft
decision or an open question.

Non-blocking follow-up design questions and deferred architecture concerns
should be added to [Backlog](./backlog.md) rather than left implicit in code or
commit discussion.

## Relationship to the Design Doc

[JJ-Native Stacked GitHub Review Design](./design.md) is the canonical source
for product behavior and policy, including:

- the review-unit and stack model
- bookmark naming and cache semantics
- submit, status, adopt, and cleanup behavior
- current command surface and scope
- fail-closed behavior when review identity is ambiguous

This document focuses on implementation choices that follow from that design:
repository layout, component boundaries, tooling, test strategy, and delivery
sequencing.

## Summary

We will build a Python client that projects a `jj` stack onto GitHub's
branch-based pull request model.

The client will:

- shell out to `jj` and `git` rather than linking to `jj-lib`
- use the `uv` toolchain for development, execution, and dependency management
- use `ty` for static type checking
- use `pydantic` for structured local and remote data models
- use `httpx` for GitHub API traffic

We will test every feature first against a local fake GitHub server backed by a
real Git repository, and then against a genuine GitHub test repository in an
opt-in live test mode.

We will develop the tool the same way we want people to review with it:
logical, self-contained, well-described stacked commits.

## Goals

1. Build a useful tool quickly without painting ourselves into a corner.
2. Keep the `jj` DAG as the source of truth for stack topology.
3. Keep GitHub integration narrow, explicit, and easy to inspect in tests.
4. Prefer end-to-end feature slices over large batches of infrastructure work.
5. Make the local fake GitHub environment the default place to develop and
   debug behavior.
6. Continuously validate the fake environment against real GitHub behavior.

## Non-Goals

Product-level scope follows the design doc. Additional implementation
non-goals for the first pass:

- support for non-GitHub forges
- a daemon or long-running background sync process
- a GUI or web UI

Reviewer and label assignment are in scope for PR creation and update flows.

## Implementation Model

At a high level, each command should follow the same shape:

1. Read local `jj` and `git` state.
2. Compute the desired review state.
3. Read relevant GitHub state.
4. Reconcile actual remote state with desired state.
5. Apply mutations in a controlled order.
6. Persist only minimal local review state and user-authored overrides.

When a stale submit intent is present, submit should refresh remote bookmark
state before re-resolving the stack and repair any matching untracked review
bookmarks whose local and remote targets already agree. That keeps reruns
resumable after an interruption in the untracked-remote update path.

We should keep the code separated along those boundaries so that planning logic
can be tested without network or subprocess side effects.

## Executable Surface

The product command surface should follow the design doc.

`land` is explicitly deferred until after the initial review lifecycle is
stable.

The tool itself should ship as a standalone executable, for example
`jj-review`.

During development inside this repo, the default invocation should be:

```text
uv run jj-review ...
```

Users may also configure `jj` aliases that delegate to the standalone
executable so that `jj review ...` works ergonomically. That alias layer should
be treated as convenience glue, not as a separate implementation surface.

For development workflows, the package may also be invoked as
`python -m jj_review`, but `uv run jj-review` should be the primary path.

Tests and packaging should target the standalone executable directly. Any `jj`
alias integration should stay thin and optional.

Command target selection should stay conservative at the CLI boundary:

- `submit` and `adopt` should require either an explicit `<revset>` or an
  explicit `--current` opt-in instead of silently defaulting to the current
  workspace path
- `cleanup --restack --apply` should require the same explicit selector
- read-only inspection may stay ergonomic, so `status` and `cleanup --restack`
  preview may still omit a selector and inspect the current path by default
- the lower-level `jj` adapter may keep its existing current-path resolution
  helper so the CLI can opt into that behavior intentionally via `--current`

## Proposed Repository Layout

Slice 1 establishes the initial scaffold using a clean layout.

Proposed shape:

```text
pyproject.toml
uv.lock
src/
  jj_review/
    __init__.py
    cli.py
    config.py
    cache.py
    models/
    commands/
    jj/
    git/
    github/
    planning/
tests/
  unit/
  integration/
  live/
  fixtures/
tools/
  fake_github/
docs/
  notes/
```

The package name is `jj_review` for now.

## Main Components

### CLI Layer

The CLI layer should be thin. It should:

- parse command arguments
- load configuration
- initialize logging
- build command dependencies
- render user-facing output and diagnostics
- for inspection-style commands such as `status` and `cleanup`, print resolved
  local context promptly and stream per-item results as remote inspection
  completes rather than buffering the full command behind one aggregate result

It should not contain stack planning logic.

Bootstrap failures such as missing config files, invalid config syntax, or bad
local paths should be surfaced as targeted CLI diagnostics rather than Python
tracebacks.

### JJ Adapter

The `jj` adapter should wrap subprocess access to `jj` and expose typed
operations such as:

- resolve a revset
- inspect the working-copy/default submit target
- enumerate the linear review chain
- read bookmarks plus tracked and untracked remote bookmark state
- surface stale-workspace errors distinctly so commands can suggest
  `jj workspace update-stale`

The adapter should prefer machine-readable template output over parsing human
text.

### Git Adapter

The Git adapter should be narrower than the `jj` adapter. We mainly need it for:

- backing repo inspection in tests
- remote branch verification
- fake GitHub server internals
- a few compatibility checks where Git is the actual remote boundary

### Planning Layer

The planning layer should be pure or as close to pure as possible. Given typed
local and remote state, it should decide:

- which changes are reviewable
- which bookmark each change should use
- which PR each change should map to
- which remote mutations are required
- which operations are hard errors

Reviewability should be computed from `jj` state, not reimplemented as
tool-local policy. In practice, that means the planner should respect the
repo's configured `immutable_heads()` boundary via `jj`'s `immutable()` /
`mutable()` semantics.

This is where most correctness should live.

### GitHub Client

The GitHub client should be a thin `httpx` wrapper plus typed `pydantic`
models.

It should know how to:

- fetch PR state
- batch PR lookup by known head branch where that avoids one-request-per-PR
- create PRs
- update PRs
- assign reviewers and labels
- manage reviewer-facing stack metadata
- perform any endpoint-specific pagination or retry behavior

It should not decide stack topology or branch naming policy.

### Config and Review State

The design doc now distinguishes user-authored config from machine-written
review state.

For now:

- config should live in `~/.config/jj-review/config.toml`
- repo-specific config should be expressed in that file with path-based
  conditional matching
- machine-written review state should live in
  `~/.local/state/jj-review/repos/<repo-id>/state.toml`
- `<repo-id>` should come from `jj`'s repo config identity; if
  `.jj/repo/config-id` is missing, the client should run
  `jj config path --repo` and then read the resulting ID
- if `jj` still cannot provide a repo config ID, commands should continue with
  review-state persistence disabled for that repo

That review state remains minimal, optional, and non-authoritative. The
implementation should model it as a sparse, versioned state file with typed
persistence.

## Data Model

We should define `pydantic` models early and use them consistently across both
the real client and the fake server.

Important model families:

- local stack models
- bookmark and remote branch models
- GitHub PR and comment models
- mutation plan models
- config and review-state file models

Important persisted records should mirror the design doc's minimal review
state:

- per-change pinned bookmark and GitHub linkage
- per-change reviewer-facing stack comment identifier, if used

Repo defaults used for resolution belong in config, not in machine-written
review state.

Command output and planning results should use first-class typed models.
Rendered output should be derived from those models rather than carrying ad hoc
dicts or stringly typed intermediate state through the command layer.

## Default Repo Resolution

For now, the common case should be zero-config. The tool should prefer
repo-derived defaults and only require explicit configuration when the repo is
ambiguous. This section extends the design doc's trunk-resolution requirement
into a full repository-resolution order.

The resolution order should be:

- selected remote: command-line flag, then local config, then `origin` if it
  exists, then the only remote if exactly one exists, otherwise fail
- trunk branch: command-line flag, then local config, then the selected
  remote's default branch if discoverable, then one remote bookmark on the
  selected remote that points at `trunk()`, otherwise fail
- GitHub owner/repo: derive from the selected remote URL, otherwise fail

Ambiguity should be a hard stop, not something the tool guesses past.

## Documenting Changes Before Coding

When we discover a design bug or a behavioral ambiguity, write down the
intended fix before implementing it.

Use these documents with a clear split:

- update `docs/notes/design.md` first if the change affects product behavior,
  persistence boundaries, invariants, or user-visible semantics
- update `docs/notes/implementation-strategy.md` if the change is primarily
  about execution strategy, staging, or component boundaries
- use the commit message to summarize what landed, not as the primary place
  where the design decision lives

For small bug fixes, a short targeted edit to the relevant section is enough.
We do not need a new note for every issue. The important thing is that the
canonical docs reflect the intended behavior before code starts depending on a
new assumption.

## Authentication

For now, the tool should resolve GitHub credentials in this order:

- `GH_TOKEN`, if set
- `GITHUB_TOKEN`, if set
- `gh auth token --hostname <resolved-github-host>`, if `gh` is installed and
  authenticated
- otherwise fail with an explicit authentication error

The application client should continue to use `httpx` directly for GitHub API
calls. If we reuse `gh` credentials, we should do so only through the supported
`gh auth token` command, not by reading `gh` config files, keychain entries, or
other internal storage directly.

## Tooling Strategy

The implementation should standardize on:

- `uv` for environment and dependency management
- `uv run` for local command execution
- `uv tool run` only where it clearly improves ergonomics
- `./check.py` as the default local verification entrypoint
- `pyrefly` for static type checking
- `ruff` for linting and formatting
- `pytest` for the test runner

## Testing Strategy

Testing is the center of the implementation strategy, not an afterthought.

For every user-visible behavior:

1. write tests first
2. implement against the local fake GitHub server
3. verify against the live GitHub test repository
4. keep the live behavior as the final arbiter

We should have three layers of tests:

- unit tests for parsing, planning, and model behavior
- local integration tests against the fake GitHub server and a real backing Git
  repo
- opt-in live tests against a genuine GitHub repository

Local tests should be the default.

The default local verification command should be:

```text
./check.py
```

That script should run `uv sync --locked`, then run `ruff check`, `pyrefly
check`, and `pytest -n auto` by default, with randomized test order so hidden
cross-test coupling is more likely to fail fast during normal local runs.

When the full suite gets slow enough to justify it, `./check.py -n 4` should
override the default worker count, while `./check.py -n 1` should provide a
serial escape hatch without changing the environment bootstrap, lint, and
type-check steps.

Coverage should be available as an explicit local verification mode:

```text
./check.py --coverage
```

That mode should keep the same `uv sync --locked`, lint, and type-check steps,
then run pytest with branch coverage enabled, emit a terminal missing-lines
report, and write an HTML report to `htmlcov/index.html` for deeper inspection
of untested code paths.

Live tests should require an explicit flag and explicit credentials.

## Fake GitHub Server Strategy

The fake GitHub server is a core part of the product development strategy.

It should:

- expose only the endpoints we currently need
- model GitHub behavior closely enough to exercise real client logic
- be backed by a real Git repository
- allow tests to assert directly on backing Git state after API calls
- evolve incrementally as new client features require more GitHub behavior

This is not a general-purpose GitHub emulator. It is a purpose-built contract
test harness for this tool.

The fake server should copy the shape and behavior of real GitHub only as far
as needed for the current slice of functionality.

We will use FastAPI for the fake server unless Starlette later proves to offer
a clear concrete advantage for this test harness.

## Fake GitHub Server Rules

To keep the fake server useful, we should follow a few rules:

- every endpoint should correspond to a real GitHub endpoint we expect the
  client to call
- fake behavior should be written to match observed GitHub behavior, not our
  preferred behavior
- when real GitHub behavior is surprising, tests should document that surprise
- if the fake server knowingly diverges from GitHub, the divergence must be
  called out in the tests and in the server code

The fake server should own a real Git repo because many assertions are about the
actual remote branch state, not just JSON responses.

## Fake GitHub Parity Tests

We should have tests for the fake GitHub layer itself to verify that its
behavior actually matches GitHub for the subset of functionality we rely on.

These tests should compare observable behavior, not implementation details. For
example:

- creating a PR creates the expected remote refs and returns the expected shape
  of JSON
- updating a PR changes the same fields GitHub changes and leaves alone the
  same fields GitHub leaves alone
- comment creation and update behave like GitHub for the endpoints we use
- branch and PR visibility in API responses match GitHub for the scenarios we
  cover

Where practical, parity tests should run the same client action once against
the fake server and once against a live throwaway GitHub repo, then compare the
resulting normalized observations.

## Live GitHub Test Strategy

The live suite should exist from early on, even if it is small.

The purpose of the live suite is not exhaustive coverage. Its purpose is to
catch fake-server drift and real-forge edge cases early.

The live suite should:

- run only when explicitly requested
- create a throwaway test repository per run
- use a dedicated namespace for temporary branches and PR artifacts
- clean up after itself as aggressively as practical
- avoid touching anything outside its namespace

The first pass should use:

```text
uv run pytest tests/live --live-github
GITHUB_TOKEN=...
JJR_GITHUB_TEST_REMOTE=origin
```

The live suite may use the `gh` CLI for throwaway repo setup and teardown if
that makes the tests materially simpler. We will not use `gh` in the main
application client.

## Development Workflow

Because we are building a stacked review tool, we should build it using stacked
review discipline.

That means:

- every implementation slice should be logically self-contained
- every commit should have a clear purpose and description
- tests for the slice should land with the slice
- any code change must pass its relevant tests before the commit is created
- docs should move with behavior, not weeks later

We should prefer a sequence like:

1. targeted design or strategy note update when behavior or assumptions change
2. failing tests
3. minimal implementation
4. cleanup/refactor if needed
5. final docs sync if user-facing behavior or usage changed

rather than:

1. large framework commit
2. large feature commit
3. delayed tests
4. delayed design clarification
5. delayed docs

## Delivery Plan

We should implement the tool in vertical slices.

### Slice 1: Project Scaffold

Status: complete.

Deliver:

- `uv` project setup
- basic CLI skeleton
- logging and config bootstrap
- test runner setup
- fake server test harness bootstrap

Done when:

- `./check.py` works locally
- a trivial fake-server integration test passes

### Tooling Follow-Up: Coverage Reporting

Status: complete.

Implemented after the initial scaffold:

- `./check.py --coverage` now reuses the standard local verification flow while
  running pytest with branch coverage enabled
- the coverage run emits both a terminal missing-lines summary and an HTML
  report under `htmlcov/`

Done when:

- coverage-enabled local verification works without bypassing bootstrap, lint,
  or type checking
- developers can inspect uncovered lines from the terminal report or the HTML
  artifact

### Slice 2: Local Stack Discovery

Status: complete.

Deliver:

- typed `jj` command wrapper
- linear stack discovery from a selected head back to `trunk()`
- rejection of unsupported graph shapes
- fail-closed handling for `trunk()` resolving to `root()`
- rejection of immutable revisions while walking the stack

Done when:

- stack discovery behavior is covered by unit and integration tests
- unsupported shapes fail with explicit diagnostics

### Slice 3: Bookmark Resolution and Review State

Status: complete.

Deliver:

- bookmark naming policy
- bookmark pinning in machine-written review state
- sparse review-state model and persistence
- separation between human config and machine-written review state

Done when:

- tests prove "generate once, then pin"
- subject changes do not churn bookmark names
- config and review state no longer live in a workspace-root sidecar file
- repo ID lookup failures fall back to generated bookmarks without persisted
  state

### Slice 4: Remote Branch Projection

Status: complete.

Deliver:

- push/move synthetic review bookmarks
- detect tracked-remote and remote branch state
- verify actual Git remote state in tests

Done when:

- tests assert on the backing Git repo after client actions
- no-op detection respects topology changes as well as content changes,
  including matching untracked remote bookmarks
- submit can update an existing untracked remote bookmark without creating a
  local bookmark conflict first

### Slice 5: PR Create and Update

Status: core complete.

Deliver:

- PR lookup
- PR creation
- PR updates
- trunk branch resolution

Deferred pending config/design work:

- reviewer and label assignment

Implemented in a follow-up:

- `submit` now also supports `--dry-run`, which resolves the stack, bookmark
  actions, push actions, and PR actions through the normal submit path while
  skipping local, remote, GitHub, cache, and intent-file mutations
- the submit CLI now prints the selected revset and remote promptly, then
  renders the final ordered review summary once the submit phases complete,
  instead of trying to stream per-revision mutation progress inline
- the per-change submit summary now renders created PRs as `[PR #n]` in live
  output and `[new PR]` in dry-run output, omitting the separate remote push
  marker because PR creation already implies the review branch was projected

Done when:

- submit works end-to-end against the fake server
- a minimal live GitHub submit test passes

### Slice 6: Reviewer-Facing Stack Metadata

Status: complete.

Deliver:

- dedicated bot comment support
- comment creation immediately after PR creation
- regeneration on every submit
- caching of comment identifiers if needed

Implemented with one dedicated PR comment per review unit, marked so `submit`
can rediscover it when cached comment IDs are missing. The comment body is
regenerated from the current submitted stack on every run and is never used as
the source of truth for topology.

Done when:

- tests prove the stack metadata is regenerated from current `jj` state
- tests prove stack metadata is not used as topology source

### Slice 7: Status and Adopt

Status: done.

Implemented in the first vertical cut:

- `status` now reports local bookmark resolution together with any discoverable
  remote and GitHub linkage, while still falling back to local-only output when
  the repo is not configured well enough for remote inspection
- `status` now prints the selected revset and remote immediately from local
  state, then streams per-change summaries in display order once GitHub
  inspection starts instead of waiting for a fully buffered status object
- local stack discovery now fetches head ancestors and their immediate
  children in bulk `jj log` queries instead of walking one parent at a time,
  which significantly reduces status startup latency on deeper stacks
- `status` now renders the `trunk()` commit as a footer row beneath the stack,
  using the same summary shape as stack entries and a best-effort trunk
  bookmark name when one can be resolved
- the CLI now supports `--time-output` as a global debugging aid that prefixes
  printed lines with elapsed time from process start
- `status` now inspects per-change GitHub linkage with bounded concurrency on
  one shared client with bounded concurrency
- `status` now derives repo-level GitHub availability from the first real PR
  lookup instead of blocking on a separate repository probe before streaming
  output
- `status` now also supports `--fetch` / `-f` to refresh remote bookmark
  observations first when the user wants a freshly fetched view before live
  GitHub inspection
- submit and `status` now persist each change's last-known PR state, and
  `status` uses that cached state to render more informative offline fallback
  summaries
- successful live `status` runs now refresh sparse cached PR linkage too, so a
  later offline run can still show last-known review identity for previously
  inspected changes
- that `status` cache refresh is now bidirectional: live observations update
  open and closed PR state, and clear cached PR linkage when GitHub reports
  that the review branch no longer has a PR
- `status` now also distinguishes merged PRs from merely closed ones and
  derives a lightweight review decision for open PRs from GitHub reviews so
  the stack summary can show approval and change-request state
- `status` now treats ambiguous GitHub PR linkage and ambiguous managed stack
  comments as incomplete inspection, so the command exits non-zero instead of
  presenting those cases as healthy output
- `status` now also prints explicit repair guidance for stale or ambiguous PR
  linkage so operators who bounced between machines can rerun `status --fetch`
  and use `adopt` intentionally instead of guessing
- `status` now also treats remote-resolution and GitHub-target fallback output
  as incomplete inspection, so local-only summaries exit non-zero when live
  inspection could not be completed
- GitHub client list endpoints now follow pagination links through one shared
  helper so status and adopt do not silently truncate multi-page remote state
- `adopt` now resolves one explicit PR number or URL against the configured
  repository, verifies that the PR is open on a same-repository head branch,
  pins that branch locally for the selected change, and persists the PR
  linkage so a later submit can update the adopted review intentionally
- `adopt` now also fails closed on GitHub lookup errors instead of surfacing
  uncaught transport exceptions through the CLI
- `adopt` now also refuses to steal an already-bound local review bookmark from
  another revision when sparse cache state is missing or stale
- slice coverage now exercises `status --fetch` as a real remote-rediscovery
  path and covers explicit `adopt` failure cases such as missing PRs, closed
  PRs, cross-repository heads, and missing remote head branches

Deliver:

- `status`
- explicit `adopt`

Done when:

- damaged linkage fails closed in `submit`
- `adopt` can attach an existing PR intentionally

### Slice 8: Cleanup

Status: done.

Implemented in the first vertical cut:

- `cleanup` now reports repo-scoped sparse-state cleanup actions before it
  mutates anything, including stale cached change records, removable managed
  stack comments on stale PRs, and stale synthetic remote review branches
- `cleanup --apply` now performs the safe subset of those actions: it prunes
  cached change entries that no longer resolve to supported local review
  stacks, deletes only managed stack comments on closed or detached PRs, and
  deletes stale remote review branches only when the remote branch is
  unambiguous and no local bookmark still owns it
- stale cache entries now avoid extra GitHub stack-comment inspection unless
  local sparse state suggests comment cleanup could still produce an action,
  such as a cached managed comment, a cached closed PR, or a missing remote
  review branch that suggests the PR may now be detached
- cleanup now overlaps the remaining GitHub stack-comment inspection with
  bounded concurrency while still applying any resulting mutations in the
  original cache-entry order
- remote-branch cleanup remains conservative and fail-closed: conflicted
  remote branches and still-present local bookmarks are surfaced as blocked
  cleanup items instead of being deleted automatically
- the fake GitHub server and GitHub client now support stack-comment deletion
  so cleanup can exercise reviewer-facing metadata removal end-to-end in the
  default integration suite

Deliver:

- stale cache cleanup
- stale reviewer-facing metadata cleanup
- conservative remote review branch cleanup

Done when:

- cleanup reports planned actions clearly
- ambiguous remote deletions are not automatic

### Slice 9: Submit Throughput

Status: done.

Implemented in the first vertical cut:

- `submit` now batches pull-request discovery by head branch through the
  GitHub GraphQL lookup path instead of issuing one REST list call per review
  unit
- `submit` now batches ordinary `jj git push --bookmark ...` updates into one
  remote push when the selected review branches can use the normal tracked
  bookmark path, while still handling untracked remote-bookmark lease updates
  conservatively one branch at a time
- once remote review branches are in place, submit now syncs PR create/update
  work with bounded concurrency, stops launching new PR work after the first
  failure, drains already-started tasks, checkpoints each successful in-flight
  PR sync, and reconciles configured reviewers and labels when PR creation or
  cache checkpointing failed partway through so reruns can converge instead of
  getting stuck half-finished
- submit-side stack-comment inspection and upsert planning now run with
  bounded concurrency, stop launching new work after the first failure, and
  checkpoint successful in-flight comment updates before surfacing the error
- the fake GitHub server now implements the GraphQL head-ref lookup path so
  the default integration suite exercises the same batched submit discovery
  flow as the real client

Deliver:

- batched submit PR discovery
- batched ordinary submit pushes
- bounded-concurrency submit PR sync
- bounded-concurrency submit stack-comment inspection

Done when:

- submit no longer performs one PR lookup request per review unit
- submit preserves fail-closed PR linkage checks under batched discovery
- submit still checkpoints cache state after each completed PR sync

### Slice 10: Merged PR Reconciliation

Status: done.

Deliver:

- persist each change's last submitted local `commit_id` in sparse review
  state on successful `submit`
- teach `status` and `status --fetch` to inspect the selected local path even
  after fetching merged PR branches has created immutable or divergent off-path
  revisions
- render merged path changes as cleanup needed instead of treating normal
  fetched GitHub merge state as a broken stack
- make `status` explain why cleanup is needed, warn that descendant submit
  operations still follow the old local ancestry, and print the exact
  `cleanup --restack` next step
- diagnose merged PRs whose base branch matches `review/*` as a GitHub policy
  problem instead of presenting them as a mysterious stack failure
- add `cleanup --restack` as the explicit opt-in local rewrite path for merged
  ancestors
- let default `cleanup --restack --apply` perform only survivor rebases whose
  destination is `trunk()`
- require `--allow-nontrunk-rebase` or manual `jj rebase` before restacking
  surviving descendants onto another surviving local review base
- keep using the selected local path rather than fetched branch-tip commits for
  merged non-trunk PRs
- leave merged or off-path artifacts alone unless some later cleanup pass can
  prove they are stale and removable

Done when:

- merge commit, squash merge, and rebase merge all show a usable status view
  after fetch because inspection follows the selected local path instead of
  failing on fetched branch artifacts
- the default status output tells the operator what `cleanup needed` means and
  what command to run next instead of making them infer the repair flow
- `cleanup --restack` restores one linear local stack of surviving review
  units by excising merged path changes from active local ancestry, while
  blocking non-trunk survivor rebases unless the operator opts in explicitly
- fetched branch-tip commits for merged non-trunk PRs are treated as projected
  remote state, not as the canonical continuation of the local stack
- automatic local rewrites fail closed only when the selected path or PR
  linkage is truly ambiguous, or when removing a merged path change would
  discard unpublished local edits
- tests cover the common fetched-merge case, safe survivor restacking, and the
  refusal cases that still require human intervention

### Future Slice: Landing

`land` is deferred until the review lifecycle is stable end-to-end.
When we revisit it, it should be planned as a separate slice because merge
policy, branch protection, and partial-stack semantics materially expand the
product surface.

The CLI contract should stay consistent with the rest of the tool:

- `jj review land [--apply] [--expect-pr <pr>] [--current | <revset>]`
- preview by default, with `--apply` required for mutations
- local-path-first target selection, with `--expect-pr` acting only as an
  optional guardrail that the operator is landing the intended review

The first implementation decision must be the landing unit. The design doc now
defines that as the maximal contiguous open prefix of the selected local path
starting at `trunk()`. The implementation should preserve that exact contract
instead of accepting arbitrary PR subsets.

The command also needs explicit phase boundaries so retries are idempotent:

1. resolve the selected local path, open-prefix boundary, trunk target, and
   GitHub linkage
2. if `--apply` is set, rerun the same planning step and abort if the plan
   changed materially since preview
3. perform the transition onto trunk using the chosen transport
4. only after that succeeds, update sparse review state and apply the exact
   PR bookkeeping for the landed prefix
5. leave broader cache pruning and stale-review cleanup to `cleanup`

Error handling should stay specific instead of collapsing everything into one
generic recovery path:

- linkage problems should point to `status --fetch` / `adopt`
- local ancestry repair should point to `cleanup --restack`
- policy or branch-protection failures should stop immediately with no fallback
- plan invalidation between preview and apply should tell the user to rerun the
  preview rather than attempting to continue with stale assumptions

Done when:

- preview output clearly identifies the landable prefix, target trunk, and any
  blocked boundary on the selected path
- apply reruns planning and refuses to continue when the path, trunk target,
  or PR/linkage state changed materially
- the chosen transport respects the design rule that review-only `review/*`
  branches are not themselves merged directly
- retries after an interrupted land are idempotent at each phase boundary
- exact post-landing bookkeeping is limited to the landed prefix, while
  broader stale-state cleanup remains a separate `cleanup` concern

### Future Slice: Stack Import

Cross-machine bootstrap and remote-stack materialization should stay separate
from both read-only refresh and local ancestry repair.

The CLI contract should be:

- `jj review import [--edit] (--pull-request <pr> | --head <bookmark> |
  --current | --revset <revset>)`
- no overloaded positional selector that could mean either a revset or a PR
- no implicit workspace motion in the default mode

The product-level split should be:

- `status --fetch` refreshes remote observations and GitHub linkage without
  mutating local review bookmarks or the workspace
- `import` materializes sparse local review state for one exact stack
- `cleanup --restack` remains the local-history repair path after merges or
  other ancestry damage

The implementation needs explicit rules for what `import` may mutate:

- refresh cache entries only for the selected stack
- create or refresh local synthetic review bookmarks only when the target is
  exact and unambiguous
- if `--edit` is requested, apply one explicit `jj`-native workspace
  transition rule and fail closed on dirty or stale workspaces
- do not rewrite commits, restack descendants, or mutate GitHub state

Done when:

- a user can bootstrap an existing review stack on a new machine from an
  explicit PR or review-branch selector
- remote-only review branches can be materialized into sparse local state
  without inventing topology from cache
- bookmark ownership conflicts, ambiguous PR linkage, and unsupported stack
  shapes fail with targeted recovery guidance
- `--edit` behavior is precise, testable, and blocked on dirty or stale
  workspaces instead of guessing what the operator meant

Backlog should keep repo-scoped `sync` as a separate question. This slice
solves explicit import/materialization, not whole-repo refresh policy.

### Future Slice: Unlink and Detached State

`unlink` should be the explicit inverse of `adopt`, but it should stay
local-only and one-change-first:

- `jj review unlink [--current | <revset>]`
- no stack-wide unlink in the initial slice
- no GitHub mutations as part of unlink itself

The key design constraint is detached-state precedence. Once a change is
explicitly unlinked, that detached record must override every other proof of
ownership:

- local synthetic bookmarks
- cached PR linkage
- discoverable GitHub linkage for the same head branch

That means the implementation cannot treat a preserved local bookmark as
sufficient proof of ownership once detached state exists.

The state model also needs to stay explicit about what is durable operator
intent versus mere cache:

- clearing cached PR fields is not enough
- unlink writes a durable detached marker for the selected change
- rerunning unlink is idempotent and should succeed as a no-op

Done when:

- unlinking one selected change clears active linkage and records detached state
- `status --fetch` surfaces detached state without repopulating active linkage
- `submit` refuses to reuse detached linkage until `adopt` clears it
- `land` rejects detached changes as not safely landable
- detached records are pruned only by explicit conservative policy, not merely
  because refresh stopped finding the old PR

## Error Handling Strategy

Errors should be explicit and actionable.

The user-visible fail-closed cases are defined in the design doc. The
implementation should classify them cleanly and surface targeted recovery
actions.

We should distinguish between:

- user/actionable errors
- unsupported-shape errors
- remote state conflicts
- fake-server parity failures
- tool bugs

When possible, diagnostics should point to the exact recovery action:

- `jj review status --fetch`
- `jj review adopt`
- `jj rebase`
- `jj review cleanup`
- `jj workspace update-stale`

## Observability

We should make the tool easy to debug without making normal output noisy.

Recommended defaults:

- concise user-facing output by default
- debug logging behind a flag
- request/response logging in debug mode with token redaction
- enough plan logging to explain why a change is being created, updated,
  skipped, or rejected

Tests should primarily assert on typed plan objects. Snapshot tests should be
used sparingly for user-facing rendered output where the exact textual shape is
part of the contract.

## Definition of Done

A feature slice is done only when all of the following are true:

- tests were written first or at least before the behavior was finalized
- the local default suite passes
- relevant live GitHub tests pass
- docs are updated if user-visible behavior changed
- the implementation lands as a logical stacked-review-quality commit

Any commit that changes code must be made only after the relevant tests for that
change are passing.

## Bottom Line

We should optimize for a tight loop:

- write a failing test
- implement the smallest real slice against the fake GitHub server
- verify the slice against real GitHub
- land it as a clean stacked commit

If we keep the `jj` DAG as the source of truth, keep the GitHub layer narrow,
and keep the fake server honest by regularly checking it against real GitHub,
the implementation should stay understandable and correct as it grows.
