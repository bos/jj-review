# jj-stack design

This document defines the product rules for `jj-stack`. See
[implementation-strategy.md](implementation-strategy.md) for architecture and
[testing-philosophy.md](testing-philosophy.md) for testing guidance. Command syntax and usage
belong in the [user guide](../README.md) and built-in `--help`.

## Summary

`jj-stack` turns a linear chain of `jj` changes into GitHub pull requests. The `jj` DAG determines
stack topology; local tracking connects each change to its PR and last submitted commit. Change
IDs and PR branch names stay stable across rewrites.

Each invocation supports one Git remote and one repo on GitHub's public API, with one PR per
change and a GitHub stack for a selection of two or more PRs. Cross-repo stacks and nonlinear
local stacks are unsupported. Inspection can still report some unsupported histories to help
users repair them.

## From local changes to pull requests

Suppose the selected local history is:

```text
trunk() <- A <- B <- C
```

`A`, `B`, and `C` are changes, and `C` is the selected head. On GitHub they become:

```text
PR for C: head jj-stack/C, base jj-stack/B
PR for B: head jj-stack/B, base jj-stack/A
PR for A: head jj-stack/A, base trunk
```

The actual branch names include a subject slug and change-ID suffix. They give GitHub stable PR
heads; the `jj` parent relation determines each PR's base.

The normal lifecycle is:

1. Use `jj` to create, reorder, split, squash, or rebase local changes.
2. Use `jj-stack view` to inspect the stack, and `jj-stack list` to see tracked stacks.
3. Use `jj-stack submit` to create or refresh the PRs for that selected stack.
4. After another local rewrite, run `submit` again; existing PRs follow their change IDs.
5. Use `jj-stack merge` to ask GitHub to merge a submitted prefix from the bottom. When GitHub
   merges immediately rather than through a queue (a **direct merge**), the same command fetches
   the result and updates the remaining local changes and pull requests.
6. After a queued merge, a merge completed outside `jj-stack`, or a native GitHub stack rebase,
   run `jj-stack sync` for that selected stack once GitHub has finished.

## Core concepts

### Change

A logical change is identified by its full `change_id`, which survives rewrites. A commit ID
identifies one immutable snapshot of that change.

Publishing requires a change in `visible()` and `mutable()`, with one visible mutable copy, a
nonblank description, a nonempty diff, and no unresolved conflicts. Recovery and inspection have
different requirements, described in their command policies below.

### Local stack

A local stack is a linear chain of changes from a selected head back to the nearest change on
`trunk()`'s first-parent chain. That trunk change is the stack's base and is not itself part of
the stack. A submitted side parent of a merge change on trunk therefore remains in the selected
path until `sync` reconciles it.

`submit --base B H` selects only the changes above `B` through `H`, written `(B, H]`. It uses `B`
as read-only base context. The [submit policy](#submit-and-branch-transport) defines the checks
and the required transition after `B` lands.

Mutating commands that select a local path require a single-parent chain. Other children
elsewhere in the DAG do not invalidate that chain. A local rewrite can propagate to those
children under ordinary `jj` rules; their PRs wait for a command selecting their own path.

`view` can show a first-parent path through a merge change with a warning. It also shows empty,
undescribed, conflicted, and divergent changes when selection can resolve them. These inspection
exceptions do not make those changes submittable.

### Tracking

A change is **tracked** when one `TrackedPR` record is saved under its full `change_id`. The
record contains:

- `PRIdentity`: the PR number and head branch name
- `SubmittedBaseline`: the exact commit last successfully submitted or explicitly adopted

These two values are created, replaced, and removed together. Partial records are invalid. The
GitHub repo is command context, not a per-change field; tracking must not be carried to another
repo. A branch name or a matching PR without a saved record does not establish tracking.

Two checks recur below:

- **Identity match**: the live PR number and head branch equal the saved `PRIdentity`.
- **Snapshot match**: an identity match whose PR head SHA also equals the submitted baseline.

An identity match establishes which PR to act on. A snapshot match also establishes which version
GitHub reports. Each command requires further checks before mutation.

### PR branches and PR bases

GitHub pull requests are branch-based: every PR needs one head branch and one base branch. The
`jj` DAG supplies neither, so `jj-stack` maintains remote branches purely as transport.

Each tracked change has exactly one Git branch used as its GitHub PR head. These branches
normally stay remote-only and outside the local `jj` view.

The initial name is:

```text
<branch-prefix>/<slug-from-subject>-<change_id.short(8)>
```

For example, with the default prefix:

```text
jj-stack/add-cache-index-ypvmkkuo
```

The slug is lowercase ASCII derived from the first description line. The change-ID suffix ties
the branch to the logical change. If two selected changes resolve to the same
name, `submit` stops.

The subject is only used once, when creating the initial name for a branch. Once a PR is
tracked, the branch name stays stable. Commands do not rename or replace it because the
description changed.

The GitHub base branch for a change is:

- the parent change's remote branch when the parent is in the local stack
- the explicit submitted base's remote branch for the bottom change selected by `submit --base`
- otherwise the trunk branch

`trunk()` defines the lower bound of a stack without specifying a GitHub branch name. GitHub's
reported default branch is used unless a different branch at local `trunk()` proves that choice
inconsistent. If GitHub reports no default, exactly one branch on the selected remote must point
at `trunk()`. A `trunk()` that falls back to `root()` cannot be resolved this way.

### The reserved branch namespace

A repo reserves exactly one branch namespace for `jj-stack`'s managed branches, named by
`branch_prefix` (`jj-stack` by default). The configured value is used as-is. Ordinary `jj`
bookmarks outside that namespace behave normally. Renaming `branch_prefix` changes only the names
of new PR branches and the fetch exclusion; a branch saved in tracking stays owned under its old
prefix.

The namespace normally stays out of the local `jj` view. `jj`'s default `immutable_heads()` counts
untracked remote bookmarks as immutable, so `doctor --fix` excludes the namespace from ordinary
fetches. Commands warn if that exclusion is missing or overridden, but use the configured fetch
selection and do not stop solely because a PR bookmark is visible.

A visible bookmark in the reserved namespace does not make its commit immutable for `jj-stack`
subprocesses, so a stack can be adopted from a clone that fetched the namespace. The exception
applies when the bookmark matches one saved PR and its submitted commit, or when the commit is
not divergent; a divergent target outside saved tracking stays immutable, so a fetched GitHub
rewrite is not mistaken for a local copy. Two untracked remote bookmarks pointing at the same
commit prevent this exception, even if both are in the reserved namespace. Trunk, tags, and
bookmarks outside the namespace still make their targets immutable. If the submitted commit and
one local rewrite are both visible, the submitted commit is treated as the submitted snapshot
rather than a second local candidate.

An unknown or mismatched bookmark creates no ownership. It remains untouched and does not block an
independent stack. `submit` refuses to claim a colliding visible name for a new PR, while live
remote target checks and exact leases continue to guard moves and deletion of tracked branches.

### GitHub stack objects

A **GitHub stack** is GitHub's server-side object for an ordered group of pull requests. This is
distinct from the local stack derived from the `jj` DAG.

A GitHub stack requires at least two pull requests. Submitting a one-change local stack therefore
creates an ordinary PR. When a later `submit` extends that stack to two or more PRs,
`jj-stack` registers the ordered PRs in a GitHub stack. An existing GitHub stack may later have
only one active member because GitHub retains merged members as history.

GitHub reports merged members as a historical bottom prefix. This document calls the remaining
members **active members**, regardless of whether an individual PR is open, draft, or closed. A
GitHub stack that lists a merged member above an active one stops only the commands that select
it; `unstack --stack <number>` still removes it.

### Workspaces

Several `jj` workspaces can share one repo, and each has its own working-copy commit. Every
workspace's working-copy commit is an ordinary commit for stack discovery when it is described
and nonempty.

Configuration and presentation reads do not snapshot the working copy. Repo operations keep
`jj`'s normal snapshot and checkout behavior.

If `jj` reports that a workspace is stale, the command stops and tells the user to run
`jj workspace update-stale`.

## Command responsibilities

The command policies below define eligibility and mutation rules. This table identifies which
command owns each operation.

| Command | Responsibility |
|---|---|
| `view` | Inspect selected local stacks and their current GitHub state. |
| `list` | List local paths with tracked changes, plus orphaned tracked PRs. |
| `submit` | Create PRs and refresh the selected stack; only this command publishes new changes. |
| `sync` | Reconcile a selected stack after a merge or native GitHub stack rebase. |
| `sync --all` | Sync stacks after merges and finish eligible PRs without local copies. |
| `merge` | Request a GitHub merge; run selected sync after a direct merge completes. |
| `unstack` | Remove a GitHub stack grouping; `--local` instead forgets local tracking. |
| `cleanup` | Remove eligible artifacts and links; optionally close explicitly selected PRs. |
| `checkout` | Adopt existing PRs and edit the selected change in the current workspace. |
| `relink` | Repair one change's link to a known PR. |
| `doctor` | Diagnose setup and local leftovers; `--fix` applies the named local repairs. |
| `in-use` | Report whether a valid tracking file exists, without creating one. |
| `completion` | Print shell completion scripts, optionally including a `jj` alias. |

`jj` owns general history editing. There is no standalone `jj-stack rebase` command.

`sync` and `merge` fetch before planning, including during `--dry-run`. A direct merge fetches
again after GitHub completes it. `checkout --pull-request` fetches when the selected PR's exact
head commit is not already local. Other commands do not fetch. **Fetched trunk** means `trunk()`
as evaluated after the command's relevant fetch; a command that does not fetch uses the locally
available trunk.

A dry run previews planned changes without applying them. It does not promise an untouched local
repo: the fetches above and ordinary `jj` working-copy snapshots can still occur.

## Safety rules, in priority order

Within the supported scope, these rules are ordered; a lower rule never weakens a higher one.

1. **Never lose work.** `jj` can undo almost any local mistake; GitHub cannot undo every remote
   mutation. Protect local changes first and treat destructive GitHub operations explicitly.
2. **Check the target.** Before changing a branch, PR, GitHub stack, or repo, confirm it is
   the intended one. Bind the mutation to that identity and version when the platform supports a
   conditional write or lease.
3. **Never guess.** Ambiguous linkage stops the command. Never guess which PR belongs to a change
   or silently adopt one that appeared in place of another.
4. **Merge what was submitted.** Merge only the exact submitted commit, using GitHub's
   expected-head check to bind the request to that commit.
5. **Respect command scope.** Stack-scoped commands mutate only selected PRs, though they may
   inspect the surrounding GitHub stack. Repo-wide cleanup and the other exceptions are listed
   under [Selection](#selection).
6. **Forget deliberately.** Remove tracking only through `unstack --local` or eligible cleanup.
   Cleanup may retire closed PRs without trunk evidence; merged local changes must first be
   reconciled by `sync`. Keep links that another local path still needs.

Most stops and warnings should also name a runnable next step when the right action is clear and
the condition is reasonably likely to occur. This UX requirement never weakens a safety rule.

## Tracking and recovery

Tracking stores no topology, desired bases, current PR state, or operation progress. Each command
observes the relevant `jj` DAG, remote refs, GitHub state, and saved tracking again. GitHub
reports PR lifecycle and stack membership; ancestry in fetched trunk establishes whether submitted
work reached this repo's trunk.

Commands do not automatically replace a tracked missing, closed, moved, or ambiguous PR. A merged
PR directs the user to `sync`; other broken links require explicit repair or cleanup. Once cleanup
removes a closed PR's tracking, `submit` can create a new PR for the change. An open untracked PR
still requires `relink`.

The first tracking write creates the repo's state file. That file remains after its last record is
removed, so `in-use` continues to report adoption. `view`, `list`, and `in-use` never create it.
An unreadable or invalid file blocks commands that load it and names the path to move aside before
using `checkout` or `relink` to restore links. A newer unsupported schema requires an upgrade;
[storage implementation](implementation-strategy.md#authority-and-stored-state) describes
migration and atomic writes.

Mutating commands serialize per repo. An interruption can leave completed external effects even
if the command reports failure. A retry computes what remains from current observations; it never
replays a saved plan or selector. The submitted baseline records an acknowledged commit, not
pending work.

## Policies

### Selection

Commands that inspect pull requests use `origin` when it exists, otherwise the sole remote.
Several remotes without `origin` are ambiguous.

Only GitHub's public API is supported. Remote URL hostnames are not validated; the path is
interpreted as a `github.com` owner and repo.

Stack lifecycle commands default to `@` when the working-copy change has a nonblank description
and contents, and to `@-` otherwise. This default does not discard an explicitly selected empty
or undescribed change; publication checks and inspection warnings apply to that selection.

`view` may accept several selectors. An arbitrary revset selects the exact commit
it resolves to as the stack head. A bare change ID, including a prefix that identifies one
logical change, or a linked pull request identifies the complete local stack containing that
change. The containing stack ends at the unique visible head descended from the selected change;
several such heads are ambiguous and selection fails closed.

When the selector is a change ID or linked PR, selection prefers the unique mutable copy outside
fetched trunk's first-parent path. A submitted side parent left by a stack merge remains
selectable until `sync`. Other commands retain their own selection boundary; for example,
`merge --pull-request` merges only through the selected PR, and `relink` requires both the change
and PR.

These modes use a different scope:

- `sync --all`, which cannot be combined with a selector
- `cleanup` without a selector, which considers every tracked change in the repo
- `cleanup --pull-request <pr>`, which may select one tracked PR whose local change is gone, and
  `cleanup --pull-request orphans`, which selects all such PRs
- `unstack --stack <number>`, which selects one GitHub stack without requiring local tracking

Apart from these modes, PR mutations stay within the selected stack. Local rewrites may still
propagate to descendants. Ambiguous selectors stop with an error.

### Identity and mutation preconditions

Before the first mutation, a command validates the identity on which every planned selected
mutation depends. This prevents a pre-existing mismatch from being discovered only after an
earlier selected PR has changed. It does not make a sequence of GitHub requests
transactional: an interruption or later GitHub rejection can leave earlier mutations in place.

The command-specific planning requirements are:

- `submit` requires an identity match for every tracked selected change and observes the exact
  remote target of each PR branch. Normally that target is the submitted baseline. It may
  already be the change's current commit after an interrupted submit, but only when the identity
  matches and the PR head agrees with that same commit. Any other target stops the command.
- `merge` requires the current local commit and remote PR branch ref both to equal
  `SubmittedBaseline.commit_id`, plus a live snapshot match. Tree or diff equivalence is not
  sufficient.
- `sync --all` requires a snapshot match before retargeting, closing, or cleaning up a PR.
- cleanup requires an identity match before closing a PR, deleting artifacts, or removing saved
  links.

When the platform supports a conditional write or lease, the mutation is bound to the identity
and version observed while planning. A renamed head, missing PR, unexpected branch target, or
competing PR found during planning stops the command and names `relink` or `unstack --local`,
depending on whether the user needs to repair or forget the saved link.

Only PR creation, `relink`, and `checkout` create or replace identity. `unstack --local`
deletes it explicitly. Cleanup is the only operation that deletes identity after checking live
evidence and removing PR artifacts; `sync` and `sync --all` invoke that operation rather than
deleting tracking themselves.

Only commands that successfully send or adopt a specific submitted commit may replace
`SubmittedBaseline` for the same PR identity:

- `submit` and `sync`, after a remaining change's PR update succeeds
- `sync`, when adopting an exact surviving GitHub stack commit
- `sync`, after replacing a GitHub-rebased stack with equivalent commits that retain the original
  change IDs
- `relink`, from the observed remote target
- `checkout`, when adopting an existing PR

The GitHub merge request itself never advances a baseline. Its automatic sync may do so under the
rules above. `cleanup`, `view`, and `list` never advance one.

### Submit and branch transport

`submit` publishes only the selected stack, bottom-up. It creates missing PRs, moves existing
PR branches, updates PR bases and content, and refreshes GitHub stack membership.

Before any remote mutation, `submit` confirms that the repo is reachable and the GitHub
Stacks API is available, then observes PRs and complete stack membership. An unavailable Stacks
API stops the command before any PR branch or pull request changes.

`submit --base B H` publishes only `(B, H]`. `B` must be an ancestor of `H` on the exact
single-parent path and is excluded from every mutation. The base is accepted only when its local
commit, submitted baseline, remote PR branch, and live PR head are the same commit; its saved
identity must uniquely match an open live PR. The bottom selected PR targets that PR branch.
A one-change selection remains an ordinary PR, while two or more selected PRs form their own
native GitHub stack.

An externally moved or missing base PR branch is never overwritten by `submit`. `jj-stack`
cannot repair it automatically: the user must externally restore that exact branch to its saved
immutable submitted commit before retrying. The retry revalidates the base from live
observations.

No boundary is stored or inferred. A later child refresh repeats `--base`; omitting it invokes
ordinary trunk-bounded submit and may include or regroup the parent path. One command never
updates the parent PR or another child. The exact named base alone controls the lifecycle:
while its PR remains open, a child refresh repeats the same `--base`; once it lands, even if a
higher change in the parent stack survives, `submit --base` stops. After syncing the parent, the
user rebases exactly the child range onto `trunk()`, runs ordinary `submit` without `--base`, and
can then merge that PR. `submit` never cascades this transition across related pull requests.

`submit` rebuilds GitHub grouping under the [membership rules](#github-stack-membership). When
only one active PR remains, it becomes an ordinary PR. A rerun observes any grouping changes that
completed before an interruption.

All selected PR branches move in one atomic push. Every update carries the exact target
`jj-stack` observed for that GitHub branch, including expected absence for a new branch. The
push binds each update to that target with an exact Git lease. If any ref moved, the whole push
fails; there is no sequential fallback. An untracked branch is accepted only for first-submit
recovery after an interrupted push: exactly one managed branch may end in the selected short
change ID, and its commit must carry the full change-ID header.

If an intermediate parent PR is not open, `submit` stops; it does not skip that parent when
choosing the child's base.

A topology rewrite counts as a PR update even when the tree diff is unchanged. During a
rewrite, `submit` may temporarily retarget selected PRs to prevent GitHub from auto-closing a PR
whose new base contains its head. An interruption may leave bases at their old value, trunk, or
the desired parent; a rerun finishes the update without replacing PRs.

An open PR currently in a merge queue is not updated. `submit` stops the selected stack before
moving any PR branch or changing any PR, and tells the user to wait for GitHub to merge it.
This does not restrict commands on independent stacks.

### Merge

`merge` considers a contiguous prefix from the bottom of the selected stack. Candidates must be
open and non-draft, with a unique visible local copy and no unresolved local conflicts. The first
draft or closed-unmerged PR blocks itself and everything above. `--pull-request` truncates the
candidate prefix at the selected linked PR.

A pull request selector still selects the complete local stack containing that PR; it changes
only the merge boundary. After a direct prefix merge, automatic reconciliation therefore covers
the unmerged changes above that boundary too, including remaining commits GitHub rewrote. An exact
revset retains its ordinary exact-head selection and cannot omit active members of
the GitHub stack.

GitHub receives one asynchronous merge request for the selected prefix, whether it contains one
PR or several. A multi-PR request acts on the matching GitHub stack. Every request passes the
exact expected head commit of the top selected PR.

Before the request, `merge` asks whether the trunk branch has a merge queue, using GitHub's merge
queue object or a `MERGE_QUEUE` branch rule. If that lookup fails, `merge` follows the ordinary
direct-merge path and lets the merge request report any policy rejection. It sends the explicit
action `merge_queue` when a queue is found and `direct_merge` otherwise.

A terminal `merged` result means a direct merge completed. `merge` then fetches and runs selected
stack reconciliation before returning. A terminal `enqueued` result means GitHub accepted the
selected PRs into the queue; it is successful but does not imply that trunk changed or that
`sync` should run yet. A rejection does not rewrite local changes; a later command observes
whatever GitHub reports.

Automatic reconciliation identifies the containing stack by the full change ID of the head
resolved before the merge request. It does not reinterpret the original revset
after fetching the changed trunk.

GitHub merge success and local reconciliation are separate outcomes. If GitHub completes the
merge but the automatic sync stops, `merge` returns the sync failure status, says that the GitHub
merge must not be retried, and leaves recovery to a later `sync`. That command rereads GitHub,
fetched trunk, the local DAG, and tracking rather than resuming saved operation state.

For a direct merge, the merge method comes from `--method`, otherwise from `merge_method` in
repo configuration, otherwise from the repo's only allowed method. GitHub reports which methods a
repo allows but never which to prefer, so a repo allowing several with none configured stops
rather than choosing one. A configured method the repo does not
allow is refused by name before any request goes out. A merge queue chooses its own method, so the
request omits it; an explicit `--method` produces a warning and is ignored.

Immediately before merging or enqueueing an ordinary PR, `jj-stack` retargets the candidate to
trunk.

Trunk advancing under a submitted stack does not itself block `merge`. GitHub decides whether the
commits and repo policy allow the merge. An ordinary PR is retargeted to trunk by branch name and
sent with its expected head commit; this does not depend on trunk staying at one commit. A one-PR
prefix selected from a larger GitHub stack remains a stack merge and is not retargeted this way.

A submitted change GitHub already merged is still a stop, decided from the pull request's own
reported state rather than from trunk position. The diagnostic names `sync`, which checks fetched
trunk before removing the local copy.

### Repo policy

A merge initiated through GitHub's UI, auto-merge, or another client is supported. Rebasing a
complete native GitHub stack through GitHub's UI is also supported. A later `sync` reconciles
either result under the rules below.

`jj-stack` does not duplicate repo policy. Apart from choosing direct merge or queue
routing for the trunk branch, it does not preflight approvals, checks, conflicts, or auto-merge
state across the repo. GitHub applies those rules to the requested GitHub stack or
single-PR mutation, and `jj-stack` reports the result.

A rejected merge must explain what the user can do next: rebase onto trunk, resolve, and submit
again for a conflict; address the failing check or repo rule on GitHub otherwise.

### Trunk evidence and sync

Two observations prove that submitted work reached trunk. GitHub reporting a pull request as
merged is not one of them, because it says nothing about the trunk this repo fetched:

- **Exact submitted commit on trunk**: the baseline is an ancestor of fetched trunk and the live
  PR is a snapshot match. A PR belonging to a GitHub stack must also report merged before `sync`
  may act on it.
- **Selected PR's rewritten merge result on trunk**: the saved PR is an identity match, reports
  merged, still reports the submitted head, and reports a merge-result commit that is an ancestor
  of fetched trunk. This covers squash and rebase results.

`sync` may use either proof. `sync --all` uses each rewritten merge result to select and reconcile
its affected local paths; it does not apply one PR's evidence to unrelated work. It continues with
independent stacks when one is blocked. If no local copy remains, it uses that evidence only for
ordinary cleanup. A native GitHub stack rebase without a merge requires selected `sync`;
`sync --all` discovers work from merge evidence.

A PR merely reporting merged, or a merge result no longer reachable from fetched trunk,
permits no change. Local changes, identity, and baseline remain untouched until a later sync can
prove the result on fetched trunk.

When an unmerged local change sits below a submitted change whose submitted work is proven on
fetched trunk, `sync` stops without mutation. Rebasing would silently decide whether that local
change belongs before or after the merged work. The diagnostic names the changes and the exact
submitted, local, and fetched-trunk commits, then gives a `jj log` command for inspecting both
histories. The user chooses the intended order with ordinary `jj`. Afterward they inspect the
remaining local pull requests, sync a remaining mutable submitted head, or run cleanup when no
submitted local copy remains.

Here unpublished local work means a mutable, non-empty change whose commit is not its submitted
baseline. An empty change modifies no files relative to its parent, so removing it discards no
content.

#### Updating local changes after a merge

`sync` reconciles the unmerged suffix only when:

- rewriting it would not discard unpublished local work
- no surviving change has multiple mutable local copies (a fetched GitHub rewrite is immutable)
- no unsubmitted change sits between remaining submitted changes
- every surviving pull request outside a GitHub stack's active members is open and still at the
  submitted or local commit; a moved or missing PR branch stops `sync` before any rewrite and
  names the repair. This preflight stop leaves local changes and PRs untouched; a later failure
  may leave completed mutations. Changes GitHub rewrote as part of a stack merge or rebase follow
  the rules below.

If any selected open PR is still in a merge queue, `sync` leaves the selected stack unchanged.
Once GitHub no longer reports it queued, ordinary trunk evidence determines whether `sync`
reconciles merged work or has nothing to do.

It rebases surviving changes onto fetched trunk even when they contain conflicts. If a submitted
change remains conflicted, the local rebase stays in place but its PR is not updated. The
user resolves the conflict with `jj` and runs `submit` for the remaining stack.

If a workspace directly has an obsolete merged change checked out, `sync` does not remove that
change. Its diagnostic identifies the workspace and gives commands to move it to trunk or forget
it and move its directory to the trash. A workspace on a surviving child does not block its
ordinary rebase.

Rewriting a selected change may also rebase its local descendants under ordinary `jj` rules. If
another local path still depends on a merged change after that rewrite, `sync` leaves the
change and its tracking in place and names each other stack that still needs `sync`. It never
updates pull requests outside the selected chain.

After updates to the remaining PRs succeed, `sync` invokes cleanup for merged pull requests that
no local path still needs. Cleanup removes each eligible PR branch and managed overview comment
before it removes the corresponding tracking. A blocked or failed cleanup leaves tracking for a
retry. A failure after the local update leaves completed work in place. Later commands observe the
current DAG, tracking, and GitHub state instead of replaying saved operation state. `sync` never
rebases merely because trunk advanced. Ordinary `jj rebase` owns that workflow. Its output
describes reconciliation and cleanup, not submission, including when no pull requests survive.

GitHub preserves `jj`'s `change-id` commit header through rebase merges of PRs, but not squash
merges. A matching full change ID on fetched trunk identifies the successor rather than
an arbitrary visible side copy. When fetched trunk has no matching change ID, `sync` retires the
old local change without relabeling that commit or storing an alias.

When a GitHub stack merge rewrites active members above the merged prefix, GitHub's rewrite of
each remaining change starts from its submitted baseline. If every remaining local change is still
at its baseline, `sync` adopts the exact commits GitHub reports rather than replaying equivalent
diffs; if any remaining change has local edits, `sync` adopts none, rebases the remaining changes
onto fetched trunk, records GitHub's reported heads as their baselines, and republishes them. It
accepts those heads and bases only while a merged tracked member of the same GitHub stack proves
the transition.

#### Native GitHub stack rebase

GitHub's native stack rebase rewrites every active member and removes `jj`'s change-ID
commit headers. With no merged member, those remote commits cannot become the identity of the
local changes. `sync` recognizes this result only when all of these observations agree:

- every member of the GitHub stack is among the selected tracked PRs, in their local parent
  order
- every PR still uses its saved head branch and the expected base branch
- every PR head and PR branch moved from its submitted baseline to the same reported commit
- the reported commits form one first-parent chain rooted at fetched trunk
- none of the selected local changes is divergent

`sync` then computes a rebase of the original local changes without first changing the local DAG.
The computed change IDs must remain the selected change IDs, conflicts are rejected, and each
computed commit tree must exactly equal the corresponding GitHub commit tree. This comparison is
also the recovery proof when a previous run integrated the local rebase but failed before moving
the PR branches.

After the proof succeeds, `sync` integrates the local rebase, atomically replaces every rewritten
PR branch using the observed GitHub heads as exact leases, and records the resulting local
commits as the submitted baselines. A changed lease leaves the local rebase in place and advances
no baseline; a retry proves its trees against the freshly observed stack. No alias from a GitHub
commit to a local change ID is stored.

### GitHub stack membership

`merge`, selected `sync`, and locally selected `unstack` require every active member of the GitHub
stack they touch to belong to the selected local parent chain. `unstack --stack` selects the
GitHub resource directly and does not require a local chain. Cleanup instead checks each candidate
and never deletes a branch needed by an active GitHub stack member.

`submit` reconciles GitHub grouping from the selected local path. It may dissolve any number of
GitHub stacks whose active members are all selected. It may also dissolve one partially selected
GitHub stack when the selection is a maximal local path and touches no other GitHub stack. A
non-maximal selection could silently truncate a still-valid stack, so it stops before mutation.
An unselected active member does not trigger that guard when its observed PR number, branch, and
head still match the saved PR number, branch, and submitted baseline, and its tracked change has
no visible off-trunk copy. That change cannot be on an extension of the selected path. Rebuilding
the grouping leaves the orphaned pull request open and retains its tracking.
Likewise, a selection that partly overlaps one GitHub stack while including any previously
submitted PR outside that resource stops; the user submits the source path first, then the
destination path.

Merged members do not have to be selected. If selected PRs appear only as history, one matching
GitHub stack may be observed without mutation; more than one is ambiguous and stops the command.

For `merge`, selected `sync`, and locally selected `unstack`, an active unselected member or two
active GitHub stacks in one selection causes a stop before mutation. The diagnostic names the
exact `jj-stack unstack --stack <number>` command when removing the grouping can unblock the
operation.

Changing the base of an active GitHub stack member requires dissolving that GitHub stack first
because GitHub offers no single-member removal. `jj-stack` asks GitHub to dissolve the exact
observed resource. If GitHub retains a queued or otherwise locked active member, the operation
stops before changing any branch or base. Historical merged members may remain in the resource;
they do not block the mutation because they are no longer active.

### Derived artifacts

A subsequent submit refreshes a PR description only if it still matches the last automated PR
description. Otherwise it is preserved; explicitly supplied text still takes effect. PR text is
not stored locally and never determines topology; see
[pull request descriptions](../reference/descriptions.md).

When a submit supplies no stack overview, the existing overview text is preserved and moves to
the current head PR if the stack grows. An explicitly supplied overview replaces it. A lone PR has
no overview comment. New PRs are created in the requested draft state.

Submit maintains a per-PR revision-history comment with the most recent versions available from
GitHub's force-push timeline. It shows readers how the PR evolved, remains after cleanup, and
never determines topology or mutation eligibility.

Without an edited draft choice, existing PRs become draft only with `--draft=all` and become ready
only with `--open`; plain `submit --draft` leaves their draft state unchanged. With `--edit`,
GitHub's current state and those command-wide defaults populate one editable draft choice per
change. The validated document then determines each selected PR's draft state without adding local
state. A newly generated editor file remains until the entire submit succeeds. If the command
stops, the user can pass that file to `--resume-edit`; the retry re-observes the local stack and
GitHub, and accepts the file only when it names exactly the currently selected changes. The file
carries no submit plan or phase.

`--reviewers` and `--team-reviewers` request the named reviewers even when a PR is otherwise
unchanged and never remove omitted reviewers. `--re-request` acts on an otherwise unchanged PR.
For each user, it considers the latest approval, request for changes, or dismissal, and requests
another review only if that state is approved or changes requested. Comment-only reviews do not
qualify. Re-requesting adds requests and never cancels a pending one.

An explicit `--label` request applies even when a PR is otherwise unchanged; configured labels
alone do not turn a no-op submit into a metadata update. Labels are also additive; omitted labels
are never removed.

Default PR bodies derived from change descriptions unfold Markdown soft line breaks into spaces.
Markdown block boundaries, code, tables, and explicit hard line breaks remain unchanged.

### Unstack and cleanup

`unstack` removes one exact GitHub stack grouping and leaves every pull request, PR branch,
overview comment, and tracking record unchanged. With `--stack <number>`, GitHub is the source of
the selected resource and no local tracking is required. Otherwise the selected local stack must
identify one coherent GitHub stack. Rerunning it after the grouping is gone is safe.

`unstack --local` removes tracking for the selected local stack without checking PR lifecycle or
trunk evidence. It leaves GitHub and local history unchanged. Cleanup, by comparison, checks
eligibility and removes PR artifacts before forgetting the link.

Closing pull requests through GitHub's UI or `gh pr close` is supported. It leaves local tracking
in place, so `submit` does not silently reuse a closed PR and `cleanup` can still prove which
branches and comments belong to it. Starting over means closing the old pull requests,
running selected cleanup, and then submitting again.

`cleanup --pull-request <pr> --close` and `cleanup --pull-request orphans --close` combine closure
and cleanup for an explicit saved selection. The flag is invalid without `--pull-request`.
Identity, PR-branch ownership, open dependents, GitHub stack membership, and the managed overview
comment are all checked before closing an open PR. Explicit cleanup first retargets the PR to
trunk so that GitHub can still reopen it once its base branch is deleted. `sync` closes a PR whose
exact submitted work is proven on trunk without retargeting: GitHub rejects changing a PR's base
to a branch that already contains its head, and reopening already-landed work protects nothing.
A PR already closed or merged skips closure and follows ordinary cleanup. A closure failure stops
later selected mutations; a rerun observes the current PR state.

Cleanup acts only on one complete identity/baseline pair, whether it runs directly or at the end
of `sync`. It may remove the managed overview comment, the saved PR branch ref at the commit
observed while planning, and the tracking record.

A pair is eligible only when:

- GitHub reports the exact saved PR closed or merged
- for a merged PR, no visible mutable local copy still needs `sync`
- no PR in the same repo that is open, or closed but still reopenable, uses the saved head ref
  as its base
- no active member of a GitHub stack still needs the branch

A closed PR counts while its own head branch still exists. GitHub cannot reopen it without a
base branch or retarget it while it is closed, so deleting its base would require restoring that
branch to reopen it. If its head branch is already absent, cleanup no longer treats that PR as a
dependent needing the base. Merged PRs do not count as dependents.

Local descendants do not substitute for the base check. A visible mutable copy of merged work is
evidence for `sync`, not deletion of a GitHub branch or tracking.

Identity and baseline are removed only after artifact cleanup succeeds. A PR that cannot be
inspected is skipped. Once mutation starts, a failure stops cleanup and leaves later records for
a rerun.

### Adoption and repair

`checkout --pull-request` treats the selected PR as the head of the remote chain to adopt and
edit. `--revset` selects an exact local head. `--pick` combines locally tracked paths with active
GitHub stack resources, showing each GitHub stack's number, top active PR, base, size, status, and
whether it is already local. Choosing a GitHub-only or partially tracked stack passes its top
active PR through the same adoption path as `--pull-request`; the picker does not create another
tracking path.

When the selected PR's exact head commit is not visible locally, `checkout` fetches ordinary
remote state and imports it through a temporary ref as a visible mutable commit; if that change
already exists locally at another commit, or the head sits above the PR's own change, `checkout`
reports the extra copies or commits and does not choose between them. It validates the complete
selected stack and saves any new tracking before it runs `jj edit` on the exact head commit it
observed. If the workspace move fails after adoption, a rerun observes the saved tracking and
retries the move. The command does not rebase changes, restack descendants, or mutate PRs, and
it leaves no PR bookmarks behind.

`relink` replaces tracking for one change after checking that the PR is open, its head branch
belongs to the same repo, and neither the PR nor branch is linked to another change. The branch
name must end in the selected change's short-ID suffix. A remote commit whose header and branch
identify another change is refused; `relink` cannot transfer a PR to a replacement change ID.

The remote target must equal the local commit or saved baseline. `--replace-remote` waives only
this check: it records the remote commit as the baseline so a later `submit` can replace it. It
does not waive identity or branch checks. The new identity and baseline are saved together.

`doctor` observes setup, GitHub Stacks API availability, and local leftovers from interrupted
`checkout` or `sync`. It changes nothing without `--fix`; its repairs are restoring the reserved
PR-branch exclusion in remote fetch configuration, forgetting untracked remote bookmarks in that
namespace, and removing the temporary import ref and bookmark. `checkout` and `sync` also remove
that leftover when they start. It never mutates GitHub.

### Inspection

`in-use` is a silent local predicate. It exits 0 when the repo has a valid tracking file and 1
when the file is absent. An invalid file or failure to locate a jj repo is an error,
not a negative result, and exits 11.

`view` and `list` are read-only. For local stack rows, both ask GitHub for current PR state
without fetching. Orphan rows in `list` show saved identity only; they do not claim to report the
PR's live state.

Both commands project paths from the local `jj` DAG. One projected path may therefore show
changes that explicit submit boundaries placed in several native GitHub stacks. Inspection does
not segment the path by GitHub resource or infer an omitted submit boundary.

Both report whether an open PR currently has a merge-queue entry. Queue presence is a transient
GitHub observation, not saved tracking; position and intermediate queue phases are not modeled.

Neither command guesses. A change with no saved PR identity is reported as not submitted,
even if a PR happens to use the branch name that change would generate. A saved PR is always
the one reported; a different open PR on its branch is a warning. An open PR whose head is
neither the local commit nor the submitted baseline is reported as moved, not as healthy.

Inspection tolerates fetched side copies of merged changes. `view` walks past immutable or
divergent side copies when a supported path remains, and shows merged local work as
`sync needed` with a `sync` hint. If no path remains, it returns a targeted selection error.

Empty, undescribed, conflicted, and merge changes produce warnings, but do not by themselves make
a report incomplete. A merge warning states that only the first-parent path is shown.

`view` and `list` share the rule for incomplete reports: an unmerged divergent change, ambiguous
PR, failed PR lookup, broken saved link, or unobserved saved PR state makes the report incomplete.
A per-change lookup failure affects only its row; a failure before rows can be built returns its
own error code. When several local changes claim one branch, `list` warns and skips live
inspection for that branch. These incomplete reports exit 10.

`view` and `submit` render stack rows through the user's `jj log` formatting. `--json`
follows [`docs/json-output.schema.json`](../json-output.schema.json) and exposes no cache state,
raw remote targets, or tracking records.

### Rewrite behavior

Most rewrites follow directly from stable change IDs and DAG-derived topology. Two cases need
explicit rules:

- **Abandon**: the change leaves every current local stack and descendants attach to its parent.
  Its PR becomes orphaned. Surviving stacks never close, reuse, or retarget it. Explicit closure
  and cleanup use `cleanup --pull-request <pr> --close`.
- **Split**: new logical changes get new change IDs and normally new PRs. The change retaining the
  original change ID retains its PR.

#### Cross-stack rewrites

- **Move changes between stacks**: submit the source path first, then the destination path. The
  first submit dissolves grouping that still includes the moved change; the second joins the
  complete destination stack. Moved changes retain their PRs and recalculate bases from their new
  parents. A trunk-based result uses ordinary `submit`; a result whose lower bound is submitted
  change `B` uses `submit --base B H`. Destination-first submission stops before mutation.
- **Split one stack into several**: each maximal linear path appears separately in repo
  inventory. The paths may contain the same observed submitted ancestors; tracking annotates those
  paths but does not put a shared PR in several GitHub stacks. The shared fork remains in its
  parent stack; each outgoing child is submitted as `(fork, head]` with explicit `--base`.
  Submitting the first bounded path dissolves the old grouping and rebuilds that path; each other
  path waits for its own submit.
- **Join several stacks into one**: submitting the resulting chain dissolves every completely
  selected GitHub stack and creates the joined grouping. It reuses pull requests by change ID,
  recalculates every base, and produces one overview comment on the new head.

Stacks not yet resubmitted may still show old overview comments. That is expected:
`submit` does not mutate stacks outside its selection. `list` identifies stale PRs across the
repo by comparing each baseline with the current change and naming the stack to refresh.
Close orphaned PRs and remove their leftovers with `cleanup --pull-request <pr> --close`.

## CLI contract

Built-in `--help` and the user guide own exact parser syntax and aliases. This specification owns
enduring selection rules, command effects, and exit meanings.

`help --all` adds advanced commands and hidden global options to ordinary top-level help.
`help --all-in-one` emits the complete command reference as deterministic Markdown; see
[documentation generation](implementation-strategy.md#documentation-generation).

Running the executable without a subcommand is equivalent to `view` without arguments.

### Exit codes

Exit codes are a public interface; their table lives in
[automation](../reference/automation.md#exit-codes) and their implementation in
[`errors.py`](../../src/jj_stack/errors.py). Codes 7–9 remain reserved for `gh stack` meanings
that have no `jj-stack` equivalent. A nonzero exit does not imply that no mutation occurred;
lifecycle commands must distinguish a planning stop from a failure after completed work.

## References

The design relies on these upstream `jj` references:

- [glossary](https://docs.jj-vcs.dev/latest/glossary/) for change IDs, rewrites, and visible
  commits
- [bookmarks](https://docs.jj-vcs.dev/latest/bookmarks/) for bookmark behavior, tracking, and
  push safety
- [GitHub workflow](https://docs.jj-vcs.dev/latest/github/) for GitHub integration and `gh`
  caveats
- [configuration](https://docs.jj-vcs.dev/latest/config/) for `jj` configuration
- [templates](https://docs.jj-vcs.dev/latest/templates/) for machine-readable template output
- [FAQ](https://docs.jj-vcs.dev/latest/faq/) for integration guidance
- [technical architecture](https://docs.jj-vcs.dev/latest/technical/architecture/) for why
  `.jj` internals are not an external extension surface
