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

Local metadata, if any, should be limited to optional GitHub linkage and user
overrides.

The result is a tool that behaves like a natural extension of `jj` instead of a
parallel stack manager.

For an MVP, the tool can be almost stateless.

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

## Design Goals

1. Make stacked GitHub PRs feel natural in a `jj` workflow.
2. Avoid storing parent/base metadata as a source of truth.
3. Keep branch names stable across rewrite-heavy review.
4. Recompute as much as possible from `jj` state on every run.
5. Keep any persisted state optional, minimal, and tool-owned.

## Proposed Mental Model

### Review Unit

A review unit is one visible mutable `jj` change, identified by full `change_id`.

That is the durable identity. Not the commit ID, not the bookmark name, and not the current diff
base.

### Review Stack

A review stack is a linear chain of review units from a selected head back to `trunk()`.

For an MVP, the tool should support only linear stacks. Reject or require manual intervention for:

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
review/<owner>/<slug-from-subject>-<change_id.short(8)>
```

Example:

```text
review/alice/fix-cache-invalidation-ypvmkkuo
```

The slug is there for reviewers using GitHub or plain Git. The short
`change_id` suffix is there so the name stays tied to the logical change
without becoming noisy.

Eight characters is a good default. It is stable, readable, and should be
effectively unique in practice once combined with the title slug. If a collision
is ever detected, the tool can extend the suffix or fall back to the full stored
bookmark name.

Once a bookmark has been created, the tool should not automatically rename it
just because the commit title changes later. Title churn should not cause branch
churn during review.

### Review Base

The GitHub base branch for a review unit is:

- the parent review unit's bookmark, if the parent is also being reviewed
- otherwise the repo trunk bookmark, resolved from `trunk()`

This is the key place where GitHub still imposes a branch model on top of `jj`.

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

If the tool stores anything locally, it should be limited to:

- optional bookmark-name override for a specific change
- optional persisted generated bookmark name, if the tool wants to pin the
  initial readable name even after the title changes
- cached PR number and URL
- last known PR state, only as a cache
- per-change user preferences such as draft or skip-review
- repo defaults such as preferred remote or GitHub owner/repo override

Even PR linkage can often be rediscovered by asking GitHub for the PR whose head
branch matches the synthetic bookmark name.

## Recommended Storage Strategy

Do not write into `jj` internals such as:

- `.jj/repo/store/extra/`
- the view/op store
- private Git ref namespaces inside the backing store

Those are tempting, but they tie the tool to storage details that `jj` explicitly keeps flexible.

For an MVP, use a tool-owned sidecar file such as:

```text
<workspace-root>/.jj-review.toml
```

Treat it as a cache and override file, not the source of truth.

That is not perfect for multi-workspace repos, but it is acceptable because the important state is
reconstructible. If multi-workspace coherence later matters, add a repo-shared cache once there is
a clean, intentional way to resolve a shared repo path.

## Submission Algorithm

Given a selected head revision:

1. Resolve the head revision, defaulting to `@-` if `@` is the empty
   working-copy commit, else `@`.
2. Walk parents back toward `trunk()`, building a linear chain of visible mutable changes.
3. Reject ambiguous shapes instead of inventing metadata to patch around them.
4. For each change from bottom to top:
   - compute the deterministic bookmark name
   - ensure the local bookmark points at the current visible commit for that change
   - ensure the bookmark is tracked on the chosen remote
   - push the bookmark
   - compute the GitHub base branch name
   - create or update the PR for `head bookmark -> base bookmark`

This bottom-up ordering matches the dependency order in the stack, and the
parent relationship is derived from the DAG rather than loaded from side
metadata.

## Rewrite Behavior

This design behaves well under normal `jj` rewrite-heavy workflows:

- Rebase: the commit ID changes, the `change_id` stays stable, and the attached
  bookmark follows the rewrite. Re-running submit updates the existing PR.
- Squash or amend: same as rebase. No separate metadata repair step is needed.
- Reorder or reparent: the stack is rediscovered from the DAG; PR base branches are recalculated.
- Abandon: `jj` deletes bookmarks attached to abandoned commits. The tool can then close the PR,
  leave it open with a warning, or mark it stale.
- Split: new logical review units get new change IDs, which should usually
  become new PRs. This is a feature, not a bug.

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

- `jj review submit [<revset>]`
- `jj review status [<revset>]`
- `jj review cleanup`

Notably absent:

- no `restack` command, because `jj` already handles descendant rewrites much better than Git
- no `track parent` command, because the parent relation comes from the DAG
- no metadata repair command, because there should be almost no metadata to repair

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

## Suggested Optional Cache Format

If a cache file exists, keep it sparse:

```toml
version = 1
remote = "origin"

[change."<full-change-id>"]
bookmark = "review/alice/fix-cache-invalidation-ypvmkkuo"
pr_number = 123
pr_url = "https://github.com/org/repo/pull/123"
draft = true
skip = false
```

Semantics:

- missing entry means "derive everything"
- present entry means "apply override or use cached GitHub lookup"
- deleting the file must never break the review stack model

## MVP Boundary

The MVP should intentionally support only:

- one remote
- one GitHub repo target
- linear stacks
- visible mutable changes
- one PR per reviewable change

The MVP should intentionally reject:

- merge commits inside the review chain
- divergent changes
- stacked reviews that cross repositories or remotes
- bookmark naming collisions caused by user overrides

## Open Questions

1. Should the default bookmark namespace include the user name, or only the change ID?
2. Should the tool derive PR titles from commit subject, full description, or a template?
3. Should abandoned or split PRs be auto-closed, or only surfaced as cleanup suggestions?
4. When part of a stack is already merged, should child PRs rebase onto the nearest submitted
   ancestor bookmark or jump directly to trunk?

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
