# JJ-Native Stacked GitHub Review Design

## Summary

This document describes a `jj`-native way to turn a linear chain of changes into
stacked GitHub pull requests without making side metadata the source of truth.

A reviewable unit should be one visible mutable change, identified by full
`change_id`.

A review stack should be derived from the `jj` DAG, not reconstructed from a
tool-owned parent map.

Each reviewable change should get one synthetic bookmark, which acts as the
GitHub PR head branch.

Local metadata, if any, should be limited to GitHub linkage, user overrides,
and the pinned bookmark name chosen for a change when the default name includes
mutable text such as the commit subject.

The result is a tool that behaves like a natural extension of `jj` instead of a
parallel stack manager.

For a first implementation, the tool can be almost stateless.

## Recommended GitHub Policy

This workflow works best when the GitHub repository is configured to reduce
branch-history shapes that do not map cleanly back into one active local stack.

Recommended settings:

- `main` should require linear history
- `review/*` should require linear history
- the repository should allow squash merges and/or rebase merges so linear
  history remains mergeable
- PRs whose base branch matches `review/*` should be blocked from merging by a
  required check or required workflow

That last rule is important. Linear-history protection by itself is not enough:
GitHub can still merge PRs targeting `review/*` with squash or rebase, which
creates accepted branch-local history that is awkward to project back into the
intended local `jj` stack model.

The intended policy is:

- PRs targeting `main` may be merged
- PRs targeting `review/*` are review-only and should not be merged directly

The tool should diagnose that policy explicitly when it sees a merged PR whose
base branch matches `review/*`. That is not a mysterious stack failure. It is a
repository-policy problem, and the user should be told that the repo should
block those merges on GitHub.

## Design Goals

1. Make stacked GitHub PRs feel natural in a `jj` workflow.
2. Be easy to use.
3. Avoid out-of-band metadata as a source of truth.
4. Keep branch names stable across rewrite-heavy review.
5. Recompute as much as possible from `jj` state on every run.
6. Keep any persisted state optional, minimal, and tool-owned.

## Relevant JJ Constraints

A few properties of `jj` drive this design:

- There is no "current bookmark". Bookmarks do not move when you create a new
  commit. They must be moved explicitly, but they do follow rewrites once
  attached to a commit.
- Bookmarks are the remote branch boundary. A local bookmark is what gets pushed
  as a remote Git branch.
- GitHub review is still branch-based. Even in a `jj` workflow, the review
  system ultimately wants a head branch and a base branch for each PR.
- `jj git push --change` can generate stable bookmark names, and `jj` already
  encourages using `change_id` as the stable ingredient in that name.
- `jj` already tracks remote bookmark positions and performs the safety checks
  needed for force-push-heavy workflows.
- `change_id` is the durable logical identity of a change across rewrites. The
  commit ID is not.
- Both `jj-lib` and the CLI are moving integration surfaces. A tool should keep
  its assumptions narrow and explicit.
- `jj`'s internal storage is not an extension API. A tool should not write into
  `.jj` internals just because they are available.

## Proposed Mental Model

### Review Unit

A review unit is one visible mutable `jj` change, identified by full `change_id`.

That is the durable identity. Not the commit ID, not the bookmark name, and not
the current diff base.

For the purposes of this tool, "visible mutable" should follow `jj`'s own
revset semantics rather than a tool-specific definition:

- "visible" means a commit in `visible()`, not a hidden predecessor reached by
  commit ID or change offset
- "mutable" means a commit in `mutable()`, with immutability determined by the
  repo's configured `immutable_heads()` and its ancestors

By default, that means the tool inherits `jj`'s notion that `trunk()`, tags,
and untracked remote bookmarks define immutable history. If a repo customizes
`immutable_heads()`, the tool should honor that customization rather than
trying to maintain its own competing definition of what is safe to review or
rewrite.

### Review Stack

A review stack is a linear chain of review units from a selected head back to
`trunk()`.

For now, the tool should support only linear stacks. Reject or require manual
intervention for:

- merge commits
- divergent changes
- multiple reviewable parents
- review trees with multiple reviewable children that must become separate PR chains

`jj` can model all of those, but GitHub stacked PR UX gets much harder once the unit is not a
simple parent-child chain.

### Review Branch

Each review unit gets exactly one synthetic bookmark branch, which becomes the GitHub PR head
branch.

That bookmark name should be readable to humans and stable for tooling.

By default, it should be derived from:

- a normalized slug from the first line of the commit description
- a short fixed-length `change_id` suffix to give stable, near-certain
  uniqueness

Example shape:

```text
review/<slug-from-subject>-<change_id.short(8)>
```

Example:

```text
review/fix-cache-invalidation-ypvmkkuo
```

The slug is there for reviewers using GitHub or plain Git. The short
`change_id` suffix is there so the name stays tied to the logical change
without becoming noisy.

Eight characters is a good default. It is stable, readable, and should be
effectively unique in practice once combined with the title slug. If a
collision is ever detected, the tool can extend the suffix or fall back to the
full stored bookmark name.

The subject slug is only an input to the initial default name. Once a bookmark
has been created, the tool should not automatically rename it just because the
commit title changes later. Title churn should not cause branch churn during
review.

That means bookmark naming is "generate once, then pin", not "recompute forever
from the current subject". The resolution order should be:

1. explicit user override, if present
2. previously chosen name discovered from local state, cached review state, or
   an existing PR for that change
3. otherwise, generate the initial default from the current subject and
   `change_id`, then persist that choice

If an implementation wants fully stateless names, it should drop the subject
slug and derive names only from stable inputs such as `change_id`.

### Review Base

The GitHub base branch for a review unit is:

- the parent review unit's bookmark, if the parent is also being reviewed
- otherwise the repo trunk branch

This is the key place where GitHub still imposes a branch model on top of `jj`.

`trunk()` still defines the stack boundary in commit space, but by itself it
does not give GitHub a base branch name. For GitHub operations, the tool must
resolve the trunk base to one concrete remote bookmark on the selected remote,
such as `main@origin`.

For now, require one of:

- an explicit repo override such as `trunk_branch = "main"`
- or an unambiguous remote bookmark on the selected remote whose target is
  `trunk()`

If `trunk()` falls back to `root()` or resolves to a commit that cannot be
mapped to exactly one remote bookmark on the target remote, submit should fail
with a configuration error instead of guessing.

### Workspaces

Review state is repo-scoped, not workspace-scoped.

That matches `jj`'s model: one repository can have multiple workspaces, each
with its own working copy, while sharing the same commit graph, bookmark view,
and operation history.

For now:

- machine-written review state should be shared across workspaces for the same
  repo
- stale working copies are a local workspace problem, not a distinct review
  concept for this tool
- if `jj` reports a stale workspace, the tool should stop and point the user to
  `jj workspace update-stale`
- divergence caused by concurrent rewrites from multiple workspaces remains an
  unsupported fail-closed case

## What Should Be Derived vs Stored

### Derive from JJ Every Time

These do not need tool-owned durable metadata:

- stack topology
- parent-child relationships
- effective diff base inside the stack
- current head commit for a change
- whether a bookmark needs to move after a rewrite
- whether a bookmark is ahead of its tracked remote

All of that already lives in the commit DAG, the change ID model, and the bookmark view.

### Store Only Optional Review State

If the tool stores any machine-written local state, it should be limited to:

- pinned generated bookmark name for a specific change, if bookmark names
  include mutable text such as the subject slug
- cached PR number and URL
- cached stack-comment identifier, if the tool uses a dedicated PR comment
- last known PR state, only as a cache
- a durable detached-linkage marker for a change the operator explicitly
  unlinked, because that is user intent the tool must not silently undo

Even PR linkage can often be rediscovered by asking GitHub for the PR whose
head branch matches the synthetic bookmark name.

User-authored settings and overrides are a separate category and should not be
mixed into machine-written review state. Those include:

- repo defaults such as preferred remote, trunk branch, or GitHub owner/repo
  override
- explicit bookmark-name override for a specific change
- per-change user preferences (future extension point; no current examples yet)

### Reviewer-Facing Stack Metadata

The tool should also maintain a reviewer-facing description of the stack on
GitHub, but that description must not be the source of truth.

For now, that can be either:

- a small stack section in each PR body
- or a dedicated bot comment on each PR

It should be regenerated on each submit from the current `jj` stack and should
list the nearest ancestor and descendant PRs in a simple human-readable order.

This serves the same UX purpose as similar stack navigation used by tools such
as `ghstack` and Graphite, but it is only presentation. The tool should not
reconstruct stack topology by reading that text back from GitHub, except
optionally to rediscover the identity of a previously created bot comment.

## Recommended Storage Strategy

Do not write into `jj` internals such as:

- `.jj/repo/store/extra/`
- the view/op store
- private Git ref namespaces inside the backing store

Those are tempting, but they tie the tool to storage details that `jj`
explicitly keeps flexible.

Do not store `jj-review` config or machine-written state in the working tree.
Tracked workspace files are the wrong default for both:

- config in the working tree looks like project-shared policy and is too easy to
  commit accidentally
- machine-written state in the working tree dirties the `jj` working copy and
  perturbs the history the tool is supposed to project

Instead, split storage into two locations:

- user config in `~/.config/jj-review/config.toml`
- machine-written review state in
  `~/.local/state/jj-review/repos/<repo-id>/state.toml`

For now, repo-specific config should live in the main user config file via
path-based conditional matching rather than a separate repo-local config file.

For machine-written review state, reuse `jj`'s repo config identity:

1. if `.jj/repo/config-id` exists, use it as `<repo-id>`
2. otherwise run `jj config path --repo` to ask `jj` to materialize its repo
   config identity
3. then read `.jj/repo/config-id` and use that as `<repo-id>`

That follows `jj`'s repo-scoped identity model without writing any
tool-specific file into the workspace.

If `jj` still cannot provide a repo config ID, the tool should continue without
persisted repo state rather than writing a fallback file into the working tree.

## Submission Algorithm

Given a selected head revision:

1. Resolve the selected head revision. When the operator explicitly asks to
   use the current path, default to `@-` if `@` is the empty working-copy
   commit, else `@`.
2. Walk parents back toward `trunk()`, building a linear chain of visible mutable changes.
3. Reject ambiguous shapes instead of inventing metadata to patch around them.
4. Resolve each change's review bookmark by reuse-first, generation-second:
   - explicit override, if present
   - otherwise previously chosen local or cached name, or the head branch of an
     existing PR for that change
   - otherwise generate the initial default name from subject plus `change_id`
     and persist that choice
   - if two selected changes resolve to the same bookmark name, fail closed
     before mutating local or remote state
5. Query GitHub for the PR state of those review branches.
   - if cached linkage and GitHub-discovered linkage disagree, stop and require
     an explicit recovery flow instead of silently creating a replacement PR
   - for now, derive the PR title from the commit subject and the PR body
     from the remaining commit description; generated stack metadata is added
     later and is not part of this slice
6. Treat merged ancestors as no longer reviewable. For each remaining change
   from bottom to top:
   - ensure the local bookmark points at the current visible commit for that change
   - treat topology changes as meaningful updates even when the patch tree is
     unchanged; if the parent review unit, bookmark target, or PR base changed,
     this is not a no-op
   - if the selected remote bookmark already points at the desired commit, treat
     it as up to date even when the local repo has not tracked that remote
     bookmark yet
   - if the local bookmark or selected remote bookmark is conflicted, stop and
     require the user to resolve the bookmark state first
   - if the selected remote bookmark exists but points somewhere else, proceed
     only if review linkage for that branch is already proven by local state,
     cached state, or GitHub discovery; otherwise fail closed instead of
     silently taking over that branch
   - when updating an existing untracked remote bookmark, do not import its
     old target into the local bookmark before the remote update completes
   - otherwise push the bookmark
   - compute the GitHub base branch name:
     - nearest ancestor in the chain whose PR is still open, if any
     - otherwise the resolved trunk branch
   - if an ancestor PR has merged and the local `jj` parentage still reflects
     the old review stack, require a local `jj rebase` before changing the PR
     base
   - create or update the PR for `head bookmark -> base bookmark`

This bottom-up ordering matches the dependency order in the stack, and the
parent relationship is derived from the DAG rather than loaded from side
metadata.

## Recovery and Repair

The tool should be conservative when review identity is unclear.

If submit cannot prove that a change still corresponds to the same review
branch and PR, it should fail with a targeted diagnostic instead of guessing.
In particular, it should not automatically open a new PR just because cached
linkage, bookmark state, or GitHub state is missing or damaged.

The recovery surface should be explicit and narrow:

- `jj review status --fetch [<revset>]` refreshes remembered remote-branch
  observations before inspecting GitHub linkage, then reports the selected
  stack and any cached or discoverable PR state without mutating GitHub or
  review bookmarks
- `jj review relink <pr> [--current | <revset>]` is an advanced repair-only
  command that explicitly reassociates an existing PR and its same-repository
  head branch with a specific `jj` change when the operator intends that
  linkage; it should pin that branch locally and persist the PR identity so a
  later submit can update the relinked review instead of opening a replacement
  PR

Mutating commands should not silently infer that target from the current
workspace path. The CLI should require an explicit selector for commands that
submit, relink, or rewrite one local review path:

- `submit` requires either an explicit `<revset>` or an explicit `--current`
  opt-in
- `relink` requires either an explicit `<revset>` or an explicit `--current`
  opt-in
- `cleanup --restack --apply` requires either an explicit `<revset>` or an
  explicit `--current` opt-in

Read-only inspection may remain ergonomic:

- `status` may omit `<revset>` and inspect the current path by default
- `cleanup --restack` without `--apply` may omit `<revset>` and preview the
  current path by default

`jj review status [<revset>]` should show the selected local stack, pinned or
discovered review bookmarks, and any cached or discoverable GitHub linkage for
those bookmarks. It is read-only with respect to GitHub and review bookmarks.
It may persist generated bookmark pins and last-known discoverable GitHub
linkage into the sparse local cache, but that cache remains advisory rather
than a source of truth.
`jj review status --fetch [<revset>]` is the same inspection command, but it
refreshes remote bookmark observations first so the report reflects the latest
remote state before it inspects GitHub linkage.
Because fetched GitHub state often produces extra visible revisions for merged
changes, status should not insist that every visible revision in the repo still
forms one supported review stack. Instead, it should discover the selected
commit-parent path, tolerate immutable or divergent off-path copies created by
fetching merged PR branches, and report the path revision for each logical
change.
If a merged pull request still appears on the selected local path, status
should continue and surface that row as cleanup needed rather than treating the
stack as broken. If refresh reveals that the selected history itself no longer
has any supported linear walk, status should fail closed with a targeted local
diagnostic rather than a traceback or an unadorned subprocess error.
Unlike `submit`, it may fall back to local-only reporting when the
repo is not configured well enough to resolve a remote or GitHub target.
Its default output should stay concise and summarize the effective review state
for each change rather than dumping cache and transport diagnostics inline.
When GitHub data is available, that summary should distinguish merged pull
requests from merely closed ones, and may surface a concise review-decision
summary such as approval or changes requested for still-open pull requests.
If GitHub is unreachable or misconfigured, status should report that once at the
repo level and then fall back to conservative per-change summaries derived from
local cache rather than claiming a PR is absent. Because that output is
incomplete, the command should exit non-zero instead of reporting success.
Likewise, if live inspection finds ambiguous PR linkage or multiple managed
stack comments for the same PR, status should surface that inline and exit
non-zero rather than silently treating the stack as healthy.
If cached PR linkage existed but GitHub now reports no PR for that review
branch, status should likewise surface that stale linkage inline and exit
non-zero before it clears the stale cached identity.
When that inspection finds stale or ambiguous PR linkage, status may also
print a short repair advisory that points the operator to `status --fetch`
for refresh and `relink` for intentional reattachment.
When cached GitHub linkage includes a last-known PR state, status may surface
that state in the fallback output as cached information rather than implying it
is live.
When live GitHub inspection succeeds, status should refresh that cached
linkage in both directions, including clearing stale cached PR identity when
GitHub now reports that no PR exists for the review branch.

When status reports `cleanup needed`, it should explain why in plain language:

- the merged PR still appears on the selected local path
- descendant submit operations will continue to follow that old local ancestry
  until the user repairs it
- the next command to run is `jj review cleanup --restack [<revset>]`, with a
  follow-up `--apply` once the preview looks correct

That guidance matters more than the raw internal distinction between "selected
path", fetched branch-tip artifacts, and off-path immutable copies. The tool
still needs those concepts internally, but the user should see an actionable
explanation rather than having to infer the repair flow from one terse label.

These commands are not sources of truth either. They are operator-driven ways
to reattach GitHub state to a `jj`-derived stack after damage, cross-machine
work, or manual edits on GitHub.

A future slice should add an explicit stack materialization command for the
cross-machine case:

- `jj review import (--pull-request <pr> | --head <bookmark> | --current |
  --revset <revset>)` fetches remote review state, resolves one exact review
  stack, and materializes sparse local review state for that stack without
  mutating GitHub

The selector should stay explicit and collision-free. In particular, the
command should not overload a bare positional argument to mean either a revset
or a PR number.

Its job is local materialization, not workspace motion:

- fetch remote bookmark observations as needed
- resolve the selected stack from a PR head branch, a specific review branch,
  or an explicitly selected local path
- refresh sparse cache entries only for that exact stack
- create or refresh local synthetic review bookmarks only when the target is
  exact, same-repository, and unambiguous

Its default job is not:

- rewriting commits
- restacking descendants
- opening, closing, or mutating PRs
- deleting local history

Failure guidance should stay narrow and specific:

- if the PR head branch is missing, cross-repository, or ambiguous, fail closed
  and explain that the selected review cannot be imported safely
- if multiple PRs match the same head branch, point the operator to
  `jj review status --fetch` and `jj review relink`
- if the fetched stack shape is unsupported locally, point the operator to
  `jj review cleanup --restack` only when the problem is local ancestry rather
  than remote identity
- if `--current` was selected but the current local path has no discoverable
  remote review linkage, say so explicitly instead of silently doing nothing
- if a local bookmark already points somewhere else, stop and explain the exact
  bookmark-ownership conflict and the safe repair steps instead of stealing
  ownership silently
- if stale local sparse state disagrees with freshly fetched linkage for the
  selected stack, fetched linkage wins only when it is exact and unambiguous;
  otherwise import should fail closed and surface the conflicting local and
  remote identities instead of partially overwriting state

`jj review status --fetch` should remain the read-only refresh path, while
`jj review import` is the explicit materialization path. A repo-scoped `sync`
command remains a separate future question rather than being folded into
either command prematurely.

A future slice should add a user-facing close command for the common
"stop reviewing this path" case:

- `jj review close [--cleanup] [--apply] [--current | <revset>]` ends active
  review for the selected local path

That command should stay local-path-first rather than PR-number-first. Its job
is to look at the selected local review path, find the managed open PRs on that
path, and then preview or apply the actions needed to end review for that path.

Without `--cleanup`, `close` should:

- close the managed open PRs for the selected path
- update local review state so those changes are no longer treated as actively
  under review
- skip already-merged or already-closed PRs on that path instead of treating
  them as new close targets
- leave local and remote review branches in place

With `--cleanup`, `close` should also perform conservative post-close cleanup
for review artifacts the tool can prove it owns for that path:

- delete owned remote review branches on the configured target remote only
- forget owned local synthetic review bookmarks
- delete managed stack comments that belong to the closed path
- remove stale managed review metadata such as cached stack-comment linkage

That cleanup should stay opt-in instead of implicit because closing PRs is less
destructive than deleting branches. Preview output should make the difference
clear so the operator can choose between "close only" and "close and clean up."
If ownership cannot be proven from exact local and remote review identity,
`--cleanup` should refuse the deletion rather than falling back to branch-name
heuristics.

`close` should also be idempotent:

- rerunning `close` on an already-closed path should succeed as a no-op or
  with a concise "nothing to close" summary
- rerunning `close --cleanup` after an earlier `close` should only perform any
  remaining safe cleanup instead of trying to close the PRs again

A future slice should also keep a repair-oriented inverse of `relink`:

- `jj review unlink [--current | <revset>]` intentionally detaches one selected
  review unit from active PR ownership without mutating GitHub

`unlink` should remain an advanced repair command, not the normal way to end a
review. Its unit of intent should mirror `relink`: one selected review unit,
identified from the local DAG.

`unlink` should clear active linkage fields such as:

- `pr_number`
- `pr_url`
- `pr_state`
- `pr_review_decision`
- `stack_comment_id`

It should then write a durable detached-linkage marker for that change. That
record matters because a plain cache clear would otherwise be undone
immediately by later rediscovery.

Detached state should mean:

- `status --fetch` may still report discovered remote bookmarks or GitHub PRs
  for the same review branch, but it must label that state as detached instead
  of repopulating active ownership
- when a preserved local bookmark still exists, status should surface it as a
  detached review bookmark rather than an active managed review branch
- `submit` must refuse to reuse detached linkage automatically, even if a local
  bookmark or a discoverable GitHub PR would normally count as proof
- `land` must reject detached changes as not safely mergeable through the
  managed review pipeline
- `relink` is the explicit way back in; it clears the detached marker and
  reestablishes active linkage intentionally

By default, `unlink` should be local-only:

- no closing PRs
- no deleting review branches
- no deleting stack comments on GitHub

It may preserve the local bookmark, but once the detached marker exists that
bookmark must no longer count as proof of active ownership. That precedence
rule is part of the product contract, not an implementation detail.

`unlink` should also be idempotent:

- unlinking an already-detached change should succeed as a no-op
- unlinking a change with no active review linkage should fail with a targeted
  diagnostic instead of creating a new detached marker for a never-linked
  change

Broader cleanup remains with `cleanup`. Detached records should not expire just
because a remote PR disappeared, but cleanup should prune detached markers
whose `change_id` no longer resolves anywhere in visible history.

## Rewrite Behavior

This design behaves well under normal `jj` rewrite-heavy workflows:

- Rebase: the commit ID changes, the `change_id` stays stable, and the attached
  bookmark follows the rewrite. Re-running submit updates the existing PR.
- Squash or amend: same as rebase. No separate metadata repair step is needed.
- Reorder or reparent: the stack is rediscovered from the DAG; PR base branches are recalculated.
- Abandon: `jj` deletes bookmarks attached to abandoned commits. The tool can then close the PR,
  leave it open with a warning, or mark it stale.
- Split: new logical review units get new change IDs, which should usually
  become new PRs. The original change still exists with the same `change_id`
  but a smaller diff; it is updated normally on next submit. This is a feature,
  not a bug.
- Ancestor merged on GitHub: merged ancestors stop acting as review bases.
  Descendants should target the nearest remaining open ancestor PR, or trunk if
  none remain. `cleanup --restack` should perform that local rewrite
  explicitly, using the selected local path as the source of truth for which
  logical changes survive.

This is exactly the kind of rewrite-heavy flow the `jj` model is good at.

## Why Not Store Parent Metadata

A branch-first review tool often needs to remember both a named parent and an
exact parent revision because the review boundary is otherwise ambiguous after
history rewrites.

In `jj`, the boundary is already represented by the commit's parent relation. The only place where
branch identity still matters is at the GitHub boundary, because GitHub wants:

- one head branch per PR
- one base branch per PR

That means the `jj` tool still needs synthetic bookmark branches, but it does
not need a saved parent graph.

## CLI Shape

The tool can stay small. A reasonable surface would be:

- `jj review submit [--current | <revset>]`
- `jj review status [--fetch] [<revset>]`
- `jj review relink <pr> [--current | <revset>]`
- `jj review unlink [--current | <revset>]` (future)
- `jj review close [--cleanup] [--apply] [--current | <revset>]` (future)
- `jj review cleanup [--restack] [--apply] [--current | <revset>]`
- `jj review import (--pull-request <pr> | --head <bookmark> | --current |
  --revset <revset>)` (future)
- `jj review land [--apply] [--expect-pr <pr>] [--current | <revset>]`
  (future)

Target selection should stay explicit:

- `submit` and `relink` require one explicit selector, either `<revset>` or
  `--current`
- `unlink` should require the same explicit selector when it is introduced
- `close` should require the same explicit selector when it is introduced
- `import` should require exactly one explicit selector when it is introduced
- `land` should require the same explicit selector when it is introduced
- `cleanup --restack --apply` likewise requires one explicit selector
- `status` and `cleanup --restack` preview may still omit both and inspect the
  current path
- passing both `<revset>` and `--current` is an error

Notably absent:

- no `restack` command, because `jj` already handles descendant rewrites much better than Git
- no `track parent` command, because the parent relation comes from the DAG
- no generic metadata repair command, because the recovery cases should stay
  explicit and narrow

## Implementation Notes

### Drive JJ via the CLI

For a first implementation, shell out to `jj` rather than linking to `jj-lib`.

Use machine-readable templates instead of parsing human log output. `jj`
templates can emit JSON, and the serialized field names and value types are
usually stable even if strict backward compatibility is not guaranteed.

That suggests commands shaped like:

```text
jj log --no-graph -r <revset> -T 'json({...})'
```

with explicit fields for:

- `change_id`
- `commit_id`
- parent commit IDs
- local bookmarks
- remote bookmarks
- description / subject

### Prefer Explicit Bookmark Control

`jj git push --change` is excellent for interactive use, but the tool should manage bookmark names
explicitly. The tool wants to be able to say:

- this change must use this bookmark name
- this bookmark must now point here
- this PR must be based on that parent bookmark

So the core primitive should be "create or move bookmark, then push bookmark", not "blindly push
change with generated name".

### GitHub Integration

A GitHub adapter can use either:

- direct GraphQL or REST calls
- `gh api` as a thin authenticated transport

If plain `gh` commands that expect a Git repo are used in a non-colocated `jj`
repo, remember that `GIT_DIR` may need to point at `.jj/repo/store/git`.

### Cleanup Semantics

`jj review cleanup` should have a concrete, conservative job:

- prune cache entries for changes that no longer exist or no longer participate
  in any review stack
- remove stale reviewer-facing stack comments that belong to closed or detached
  PRs
- optionally delete synthetic remote review branches only when they are clearly
  stale, such as after the corresponding PR is closed or the change has been
  abandoned

For now, cleanup should prefer reporting planned actions before mutating
remote state. Deleting open PRs or deleting review branches for ambiguous cases
should require explicit user intent rather than happening automatically.

`jj review cleanup --restack` is the explicit local-history repair path for the
common case where GitHub merges have been fetched and the local stack still
contains merged review units.

Its UX should be explicit:

- without `--apply`, it previews the local restack plan
- with `--apply`, it performs only the restack steps whose destination is
  `trunk()`
- if a remaining restack step would rebase onto another surviving review unit,
  it should stop and tell the user to either rebase manually with `jj rebase`
  or rerun with `--allow-nontrunk-rebase`
- if repo policy is part of the problem, it should say so directly instead of
  making the user reverse-engineer it from the DAG

Its job is to restore one active local linear stack from three inputs:

- the selected local commit-parent path
- GitHub PR state for review bookmarks
- sparse local review state, including the last submitted local `commit_id`
  for each change

It should not treat every fetched remote branch tip as local ancestry that must
be preserved. GitHub is authoritative about PR outcomes and remote branch tips,
but the selected local path remains authoritative about which logical changes
are still part of the user's active stack.

The algorithm should be:

1. Discover the selected local path from the requested head back toward
   `trunk()`, tolerating immutable or divergent off-path revisions created by
   fetching merged PR branches.
2. For each logical change on that path, classify its PR state as open,
   merged, closed-unmerged, or absent.
3. Treat only merged path changes as removable review units. Open and absent
   path changes are survivors. Closed-unmerged path changes are not rewritten
   automatically.
4. For each survivor, compute its desired new parent in logical order:
   - the nearest earlier survivor on the selected path, if any
   - otherwise the current `trunk()`
5. Rebase each survivor segment whose current parent is a merged path change
   onto that desired new parent, but allow default `--apply` to perform only
   the steps whose destination is `trunk()`
6. If later survivor segments would still need to land on another surviving
   review unit, stop and require either manual `jj rebase` or an explicit
   `--allow-nontrunk-rebase` override
7. After the rebases succeed, the implementation may leave merged or fetched
   off-path artifacts in place until a later conservative cleanup pass can
   prove they are stale and removable. Restack's primary job is to repair the
   active local path first.
8. Do not rebase surviving local descendants onto fetched branch-tip commits
   for merged non-trunk PRs. Those fetched commits are projected branch state,
   not the canonical continuation of the active local stack.

This keeps the local result as close to linear as possible:

- merged review units disappear from the active path
- surviving open review units stay in order
- unsubmitted local work above them stays attached to the nearest surviving
  base
- fetched off-path copies of merged changes may remain as stale artifacts, but
  they no longer define the active stack

`cleanup --restack` should fail closed only when it cannot prove what the
selected path means. In particular, it should stop with a targeted diagnostic
when:

- the selected path itself is not a supported linear walk
- a path change has ambiguous PR linkage
- a merged path change has local edits since its last submit and removing it
  would discard unpublished work
- a closed-unmerged path change would need to be skipped or removed, because
  that policy is user intent rather than automatic cleanup

It should not stop merely because fetched GitHub merges created extra visible
revisions or moved review branches to merge commits.

### Landing and Merge Lifecycle

`jj review land` is the terminal operation for a reviewed local stack, but it
should still stay local-path-first and `jj`-native.

The selected local `jj` path remains the source of truth. `land` must not
silently repair topology, invent ancestry from GitHub, or treat review
branches as the canonical landed history.

Its default UX should mirror the preview-first shape already used by
`cleanup`:

- without `--apply`, it prints the landing plan, the landable review unit, the
  target trunk, and any exact follow-up bookkeeping it can already prove safe
- with `--apply`, it reruns the same planning step and stops if the plan has
  changed materially since preview
- `--expect-pr <pr>` is an optional guardrail, not the primary selector; it
  asserts that the selected local path still corresponds to the PR the
  operator intended to land

The landing unit should be one precise thing: the maximal contiguous open
prefix of the selected local path starting at `trunk()`.

That means:

- walk the selected local path upward from `trunk()`
- include consecutive review units whose PRs are still open and whose linkage
  is unambiguous
- stop at the first merged, closed-unmerged, missing, or ambiguous review unit
- if the resulting prefix is empty, report that nothing is currently landable
  on the selected path

This is intentionally not "the entire selected stack no matter what" and not
"whatever open PR the operator typed". It keeps the command aligned with the
local DAG and avoids partial-stack guesses.

This design also needs to respect the recommended GitHub policy above:

- PRs targeting `review/*` are review-only and should not be merged directly
- `land` should replay the corresponding local prefix onto the trunk branch
  locally in `jj`, preserving the landed prefix as a stack of commits rather
  than collapsing it into one squashed trunk result
- after producing that local landed result, it should update the trunk branch
  by pushing the new trunk tip with an optimistic lease that still respects
  repository policy and branch protection
- `land` merges onto trunk, not into synthetic review bases, and it does not
  delegate the history shape to GitHub's PR merge UI
- that bypass is intentional: trunk branch protection and required checks gate
  landing, while `review/*` protection exists to block accidental direct merges
  into review branches rather than to act as the landing gate

That means `land` owns the merge transition for the landed prefix, while
`review/*` branches remain projected review state rather than merge targets in
their own right.

Preview output should become invalid if any material planning input changes
before `--apply`. At minimum, apply should rerun planning and stop if any of
these changed:

- the selected revset or `--current` resolution
- the selected path's `change_id`s or `commit_id`s
- the open-prefix boundary
- the expected PR, if `--expect-pr` was supplied
- the trunk target or trunk commit
- the GitHub PR states or linkage for the landing unit

Recovery guidance should stay case-specific:

- if PR linkage is missing or ambiguous, point the operator to
  `jj review status --fetch` and `jj review relink`
- if the open-prefix scan stops at a closed-but-unmerged PR, say so directly
  and tell the operator to close or clean up that review path before retrying
- if the selected path itself needs local ancestry repair after an earlier
  merge, point the operator to `jj review cleanup --restack`
- if the selected path has no landable prefix, say so directly and explain
  whether the user should select a different head, clean up merged ancestors,
  or repair closed PR state first
- if repository policy or branch protection blocks the transition onto trunk,
  surface that as a hard error instead of trying an alternate mutation path

`land` should own only the exact bookkeeping that follows directly from the
successful landing transition:

- record enough intent and result state to resume idempotently if the command
  is interrupted
- update local sparse review state for the landed prefix
- close or mark landed only the PRs that correspond exactly to the landed
  prefix, once the trunk transition succeeds
- apply that PR finalization bottom-to-top through the landed prefix so the
  GitHub-side state changes follow the same stack order as submission and
  landing
- if there are surviving descendants above the landed prefix, tell the
  operator to repair local ancestry with `jj review cleanup --restack` and
  then rerun `submit`; `land` should not silently retarget or restack those
  surviving descendants itself

Broader cleanup remains the job of `cleanup`:

- pruning stale cache entries outside the landed prefix
- deleting stale review branches or stack comments not proven to belong to the
  just-landed prefix
- removing fetched off-path artifacts
- any ambiguous or indirect repair that still needs operator confirmation

## Suggested Review State Format

If a machine-written review state file exists, keep it sparse:

```toml
version = 1

[change."<full-change-id>"]
bookmark = "review/fix-cache-invalidation-ypvmkkuo"
detached_at = "2026-03-22T12:34:56+00:00"
link_state = "active"
pr_number = 123
pr_review_decision = "approved"
pr_state = "open"
pr_url = "https://github.com/org/repo/pull/123"
stack_comment_id = 456789
last_submitted_commit_id = "0123456789abcdef"
```

Suggested path:

```text
~/.local/state/jj-review/repos/<repo-id>/state.toml
```

Semantics:

- missing entry means "reuse any discoverable bookmark or PR state, otherwise
  generate defaults"
- present entry means "reuse cached generated state if still consistent"
- if the machine-written state file is unreadable or partially written, treat
  it as missing cache state for recovery purposes, warn once, and fall back to
  rediscovery where the command can do so safely
- `link_state = "detached"` is durable operator intent and suppresses
  automatic reattachment until the user runs `relink`
- cached `pr_state` and `pr_review_decision` are advisory last-known GitHub
  observations for status rendering, not a source of truth
- deleting the file must never break the review stack model, though it may
  force rediscovery or manual reattachment of review bookmarks

Suggested config path:

```text
~/.config/jj-review/config.toml
```

User-authored per-change overrides such as `bookmark_override` belong in
config, not in the machine-written state file. Additional per-change
preferences are a future extension point.

## Current Boundary

The current design intentionally supports only:

- one remote
- one GitHub repo target
- linear stacks
- visible mutable changes
- one PR per reviewable change

The current design intentionally rejects:

- merge commits inside the review chain
- divergent changes
- stacked reviews that cross repositories or remotes
- bookmark naming collisions caused by user overrides

## Open Questions

1. Should the tool eventually support PR title/body templates beyond the raw
   commit description mapping used today?
2. Should abandoned or split PRs be auto-closed, or only surfaced as cleanup
   suggestions?

## Bottom Line

The central insight is simple:

In a branch-first review tool, stack metadata often becomes part of the core
model.

In `jj`, the stack model is already the commit DAG. The tool's job is only to
project that DAG onto GitHub's branch-based PR API with stable synthetic
bookmarks.

## References

The design above relies on a small set of `jj` concepts and docs:

- `docs/glossary.md` for `change_id`, bookmarks, rewrites, and visible commits
- `docs/bookmarks.md` for bookmark behavior, tracking, and push safety
- `docs/github.md` for the current GitHub workflow and `gh` caveats
- `docs/config.md` for generated bookmark names on `jj git push --change`
- `docs/templates.md` for machine-readable template output
- `docs/FAQ.md` for guidance on integrating with `jj`
- `docs/technical/architecture.md` for why `.jj` internals should not be
  treated as an external extension surface
