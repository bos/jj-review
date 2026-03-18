# Claude Code Instructions

## Version control

This repository uses **jj (Jujutsu)** for version control. jj operates as a
layer on top of the underlying git repo, but the two are not interchangeable.

**Never use `isolation: "worktree"` when spawning subagents in this repo.**
Git worktrees branch from git commits and are invisible to jj. Any uncommitted
jj changes — which live in the jj operation log, not in git commits — will be
absent in a worktree. Agents working in a worktree will silently base their
work on an older state, producing changes that must be manually reconciled
against the real working copy.

For sequential workflows (plan → implement → review), subagents should work
directly in the repository root (`/Users/bosullivan/dev/cod`) without
isolation. The subagent modifies files; the orchestrator reviews and commits.

When true isolation is needed (e.g. two subagents working in parallel), use
**jj workspaces** instead. jj workspaces share the full operation log and can
target any revision including the current `@`, so the subagent sees the real
working state:

```bash
jj workspace add /tmp/subagent-ws --revision @  # create
# ... subagent works and commits in /tmp/subagent-ws ...
jj rebase -s <subagent-head> -d @               # incorporate
jj workspace forget subagent-ws                  # clean up
```
