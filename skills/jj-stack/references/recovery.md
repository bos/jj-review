# Recovery workflows

Read this file for abnormal lifecycle state, repo-wide reconciliation, adoption, repair,
or cleanup beyond the common flow. Keep the universal safety and GitHub-write rules from
`SKILL.md` in force.

## Observe before choosing a repair

Run `doctor`, then inspect with explicit targets:

```text
list --json
view --pull-request <pr> --json
view <head-change-id>
```

`view` and `list` may exit 10 with valid output when inspection is incomplete;
read the JSON before deciding. After interruption or in a multi-stack repo, never rely on
the default selection. Preview the chosen mutation with `--dry-run` when supported.

Do not resume a remembered plan. Every retry must use current `jj`, tracking, remote, and GitHub
observations. Use `jj op log` and `jj undo` for local recovery, never destructive Git commands.

## Reconcile GitHub changes

- After a queued or external merge finishes, run `sync --dry-run <head-change-id>`, then
  `sync <head-change-id>`. It fetches, proves what reached trunk, removes merged ancestors,
  rebases selected survivors, and updates only their existing PRs.
- After GitHub's **Rebase stack** action finishes, use the same selected `sync` sequence. It
  verifies the rewritten PR branches and contents, rebases the original local changes, and
  restores their change IDs.
- Use the full head change ID when a rewritten survivor has several visible commits. Let the
  selected `sync` prove which commit GitHub produced; do not choose a `/0` or `/1` copy or
  abandon a copy before that dry run.
- Do not run a separate `jj git fetch` merely to prepare this recovery. `sync` performs the
  required fetch itself; importing rewritten PR branches first can create avoidable local
  divergence.
- If a direct merge completed but automatic sync failed, do not rerun `merge`; continue with the
  explicit selected `sync` printed by the command.
- If a queued PR is still waiting, do not submit or sync that stack. Independent stacks remain
  usable.
- If trunk merely advanced, none of the stack merged, and GitHub left every PR branch alone, use
  a bounded plain `jj rebase`; `sync` is not a general trunk-refresh command.

Use `sync --all --dry-run`, then `sync --all`, only for repo-wide reconciliation. It finds each
local stack affected by a completed merge and applies the same selected `sync` workflow in turn.
It may therefore rebase surviving changes and their descendants, update or close pull requests,
delete unused PR branches and overview comments, and remove saved links. It never creates a pull
request. A blocked stack does not prevent independent stacks from continuing; inspect its
diagnostic before retrying it with an explicit selector.

## Recover an interrupted or rejected operation

After an interrupted `checkout` or `sync`, run `view` with the original explicit selector and
retry the same command from current observations.

For a rejected merge, fix the reported check, conflict, policy, or access problem. Rerun the same
explicit selector and merge method only when GitHub did not complete the merge. If a matching
request is pending, wait and observe it rather than starting another request.

## Adopt, repair, or forget tracking

- Use `checkout --pull-request <pr>` to fetch as needed and adopt the existing stack through
  that PR, then edit the selected PR's change without rebasing changes or touching GitHub.
- Use `relink <pr> <revset>` when one known open PR and PR branch must be attached to one
  existing local change.
- Use `unstack --local <head-change-id>` only to forget saved links without changing GitHub,
  PR branches, PRs, or local history.

When recovering known lost tracking, inspect and adopt the known PR before any direct GitHub
mutation.

Do not overwrite or recreate a missing, moved, foreign, or ambiguous PR branch. Follow the
reported `relink` or `unstack --local` path. If a direct structural GitHub mutation already
happened, inspect first and choose among `checkout`, `relink`, `submit`, `unstack`, or `cleanup`
from observed state; never rebuild changes or PRs by hand.

## Repair grouping and uncommon cleanup

When GitHub grouping no longer maps to one local path, preview the exact stack number from the
diagnostic with `unstack --dry-run --stack <number>`, then run `unstack --stack <number>`. This
removes grouping only and leaves PRs open. Never guess a stack number.

To start fresh PRs for the same changes, follow the closing and cleanup procedure in
`SKILL.md`, then run `submit <head-change-id>`. There is no restart flag; submitting before
cleanup does not replace the saved PRs.

For an orphan reported by `list`, inspect its exact PR and verify its current live state. If the
user wants closure and cleanup, run `cleanup --pull-request <pr> --close --dry-run`, then
`cleanup --pull-request <pr> --close`. Resolve any grouping or dependent-PR blocker named by the
preview before retrying. Already closed or merged PRs do not need `--close`.
Use `cleanup --pull-request orphans --close` only when the user requested all saved orphans, and
preview that same selection first. Orphan rows come from saved tracking and do not by themselves
prove live PR state.

## Diagnose local setup

Use `doctor --fix` only when a diagnostic names a repo setup defect that blocks the
requested task. It can restore the normal fetch exclusion for the reserved PR-branch
namespace. A visible PR bookmark is acceptable when it matches saved tracking state; repair
only a collision or mismatch named by the affected command. Do not run `doctor --fix` after a
successful operation as general cleanup. Use `doctor` for authentication, remote resolution, and
interrupted checkout or sync leftovers.
