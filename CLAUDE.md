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

Subagents that need to read or modify code should work directly in the
repository root (`/Users/bosullivan/dev/cod`), without isolation.
