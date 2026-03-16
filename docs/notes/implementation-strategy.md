# JJ-Native Stacked GitHub Review Implementation Strategy

This document describes how we intend to implement the stacked GitHub review
tool described in [JJ-Native Stacked GitHub Review
Design](./design.md).

It is intentionally pragmatic. The goal is to describe how we will build the
tool, how we will test it, and how we will stage the work into reviewable
commits.

Anything marked `XXX` is not fully cooked yet and should be treated as a draft
decision or an open question.

## Relationship to the Design Doc

[JJ-Native Stacked GitHub Review Design](./design.md) is the canonical source
for product behavior and policy, including:

- the review-unit and stack model
- bookmark naming and cache semantics
- submit, sync, adopt, and cleanup behavior
- MVP command surface and scope
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

1. Build a useful MVP quickly without painting ourselves into a corner.
2. Keep the `jj` DAG as the source of truth for stack topology.
3. Keep GitHub integration narrow, explicit, and easy to inspect in tests.
4. Prefer end-to-end feature slices over large batches of infrastructure work.
5. Make the local fake GitHub environment the default place to develop and
   debug behavior.
6. Continuously validate the fake environment against real GitHub behavior.

## Non-Goals

Product-level MVP scope follows the design doc. Additional implementation
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
- create PRs
- update PRs
- assign reviewers and labels
- manage reviewer-facing stack metadata
- perform any endpoint-specific pagination or retry behavior

It should not decide stack topology or branch naming policy.

### Config and Review State

The design doc now distinguishes user-authored config from machine-written
review state.

For the MVP:

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

For the MVP, the common case should be zero-config. The tool should prefer
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

For the MVP, the tool should resolve GitHub credentials in this order:

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
- `ty` for static type checking
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

That script should run `uv sync --locked`, then run `ruff check`, `ty check`,
and `pytest` in sequence.

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

We should implement the MVP in vertical slices.

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

### Slice 7: Status, Sync, and Adopt

Status: in progress.

Implemented in the first vertical cut:

- `status` now reports local bookmark resolution together with any discoverable
  remote and GitHub linkage, while still falling back to local-only output when
  the repo is not configured well enough for remote inspection
- `sync` now refreshes cached PR metadata and managed stack-comment IDs from
  GitHub for already-linked review branches, refreshes remembered remote state
  first, and fails closed when cached linkage is ambiguous or damaged instead
  of silently repairing it

Remaining for the slice:

- explicit `adopt`

Deliver:

- `status`
- explicit `sync`
- explicit `adopt`

Done when:

- damaged linkage fails closed in `submit`
- `sync` can refresh cached PR metadata
- `adopt` can attach an existing PR intentionally

### Slice 8: Cleanup

Deliver:

- stale cache cleanup
- stale reviewer-facing metadata cleanup
- conservative remote review branch cleanup

Done when:

- cleanup reports planned actions clearly
- ambiguous remote deletions are not automatic

### Post-MVP: Landing

`land` is deferred until after the MVP review lifecycle is stable end-to-end.
When we revisit it, it should be planned as a separate slice because merge
policy, branch protection, and partial-stack semantics materially expand the
product surface.

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

- `jj review sync`
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
