# User-Facing Help and Docs Plan

A systematic plan for bringing `jj-review`'s built-in help and `docs/` tree to parity
with (and beyond) Graphite's `gt` CLI and GitHub's newly released `gh stack`. Audience
assumption: mild-to-moderate jj and git knowledge, zero knowledge of this tool's
internals.

This document is the source of truth for _what we're aiming for_ and _what's missing_.
Once an item here is shipped, move it from this file to a one-line note in the
appropriate doc (e.g. `implementation-strategy.md` for user-visible surface changes) and
delete it here. This file should shrink over time.

## 1. Current state vs. the field

| Axis                        | jj-review today                       | `gt`                         | `gh stack`                                          |
| --------------------------- | ------------------------------------- | ---------------------------- | --------------------------------------------------- |
| Short help style            | Outcome-focused, verb-first ✓         | Same ✓                       | Same ✓                                              |
| Inline `--help` examples    | None                                  | None                         | Every command has an Examples block                 |
| Per-command help template   | Inconsistent                          | Inconsistent                 | Strictly uniform                                    |
| Exit-code vocabulary        | Binary (0/1 from CliError)            | Binary                       | Semantic table (2 Not in stack, 3 Conflict, …)      |
| Quick start                 | In repo README, not in `docs/`        | 5 numbered steps, 1 cmd each | 6 numbered steps, no branching                      |
| Concept intro               | Dedicated `mental-model.md`           | Inline callouts in each how-to | Dedicated Overview + ASCII diagram reused        |
| Troubleshooting             | `troubleshooting.md` by symptom ✓     | Woven into how-tos, no index | Exit-code table only                                |
| Diagnostic command          | `doctor` ✓ (unique)                   | —                            | —                                                   |
| Undo / recovery             | `abort` ✓ (unique)                    | `gt undo`, `gt abort`        | `rebase --abort`, `unstack --local`                 |
| Adopt existing PRs          | `import` ✓                            | `gt track`                   | `init --adopt`                                      |
| Safe vs. interactive split  | Not surfaced                          | Not surfaced                 | `sync` safe/auto-abort; `rebase` is where you touch |
| Abstraction leaks named     | Implicit                              | Implicit ("idempotently")    | Named inline at the command                         |
| Stack diagram               | None                                  | None                         | ASCII diagram, reused on multiple pages             |

Unique strengths to protect: `doctor`, `abort`, `import`, symptom-keyed troubleshooting,
vocabulary discipline enforced by `docs/AGENTS.md`.

Biggest gaps: inline `--help` examples, a uniform per-command help template, semantic
exit codes, a visual stack diagram, a 90-second quick start, and end-to-end
jj↔GitHub round-trip coverage (change-ID evolution across amend/rebase, server-side
"Rebase" clicks, reviewer suggestions pushed from GitHub).

## 2. Design principles

1. **Two audiences, one surface.** `--help` is for the user at the terminal who needs
   _this command now_. `docs/` is for the user with a question that isn't "what does
   this flag do". Never duplicate — cross-link.
2. **Outcome-first, one sentence.** Every short help starts with a verb and ends with a
   period. If it starts with "The" or "A", it's wrong.
3. **Assume jj/git fluency; forbid internal jargon.** Keep the existing AGENTS.md
   vocabulary discipline. Load-bearing terms — _stack_, _trunk_, _review branch_,
   _tracked change_, _ready prefix_ — defined once in a glossary, linked by name
   everywhere else.
4. **Name the abstraction leaks.** When we force-push, we say so. When ambiguity fails
   closed, we say so. When the jj op log is the recovery mechanism, we link it. `gt`'s
   "idempotently" doing all the work is the failure mode to avoid.
5. **One recipe per user intent, as a literal transcript.** Not prose. Not "you could
   also…". Copy-pasteable.
6. **Safe-by-default commands and interactive-repair commands are different commands**,
   not flag variants. Follow `gh stack`'s `sync`/`rebase` split.
7. **Every help text is greppable and uniform.** Same section order, same flag-table
   shape. Enforce with a test.

## 3. Plan for `--help`

### 3a. Per-command help template (mandatory)

```
<one-sentence outcome, verb-first, period>

Usage: jj-review <cmd> [flags] [<revset>]

<1-4 sentence description — covers pre-conditions, side-effects, and the
 abstraction leak (force-push, GitHub auth, op-log entry) if any>

Options:
  --flag VALUE    One-line outcome. Default: X.

Examples:
  # Submit the stack from @ up to trunk.
  jj-review submit @

  # Re-submit only the top change after amending it.
  jj-review submit @

See also: jj-review status, jj-review land. docs: jj-review docs submit
```

Same section order for every command: Synopsis / Usage / Description / Options /
Examples / See also. Highest-leverage change in this plan: it makes `--help` scannable
and eliminates "when do I use this vs. that" through the See-also line.

### 3b. Inline examples

Add 2–3 realistic examples to every command's long help. For `submit`, `land`,
`status`, `abort`, `cleanup`, `import`, cover the normal path plus the most common
not-the-normal-path (re-submit after amend, land with `--pull-request`, import by
`--revset` vs `--pull-request`). Draw from `docs/daily-workflow.md` — don't invent.

### 3c. Pre-condition banners

At the top of the Description for any command that talks to GitHub (`submit`, `status`,
`land`, `close`, `import`): one line — `Requires: GitHub auth (GITHUB_TOKEN or gh
CLI).` One line. No apology.

### 3d. Semantic exit codes

Adopt a small exit-code table. Proposed:

| Code | Meaning                                                    |
| ---- | ---------------------------------------------------------- |
| 0    | success                                                    |
| 1    | generic error (catch-all)                                  |
| 2    | not a jj repo / no jj-review state                         |
| 3    | GitHub auth / API failure                                  |
| 4    | ambiguous linkage (fails-closed case)                      |
| 5    | stack divergence (local/remote disagree, surfaced by status) |
| 6    | interrupted operation detected (suggests `abort`)          |
| 7    | user input error (bad revset, bad flag combo)              |

Publish in `docs/reference/exit-codes.md` and reference from `doctor` output. Payoff:
scripting users can branch on these, and existing actionable error messages become
machine-readable.

### 3e. `jj-review docs [<topic>]` command

Small new command (~30 LOC). Opens the shipped docs — either `xdg-open`-ing the docs
site URL if we publish one, or `less`-ing the bundled markdown. Lowers friction between
"I ran `--help`" and finding the right how-to. Borrowed directly from `gt docs`.

### 3f. Visibility of hidden commands

Currently hidden: `relink`, `unlink`, `completion`, and the globals `--repository`,
`--config`, `--config-file`, `--debug`, `--time-output`. Keep most hidden but:

- Promote `unlink` to visible — users genuinely need the soft detach.
- Mention `relink` in `abort`'s See-also line — it's the escape hatch from a bad
  `import` or partial land.

## 4. Plan for `docs/`

### 4a. Target information architecture (3 levels max)

```
docs/
  README.md                  (index — links, one-paragraph "what this is")
  quickstart.md              NEW — 90-second, 5 steps, one command each
  concepts.md                RENAME from mental-model.md; add ASCII diagram
  glossary.md                NEW — 5-8 load-bearing terms, one line each
  workflows/                 NEW dir (replaces daily-workflow.md)
    submit-and-iterate.md
    land-and-cleanup.md
    adopt-existing-prs.md    (import scenarios)
    respond-to-review.md
    restructure-the-stack.md (split/merge/reorder changes mid-review)
  troubleshooting.md         KEEP — already best-in-class; extend (§5)
  reference/
    commands.md              NEW — one-liner per command, flags delegated to --help
    exit-codes.md            NEW — semantic table (§3d)
    compatibility.md         NEW — jj version matrix, gh version, auth paths
  internals/                 UNCHANGED
```

Four deliberate choices:

1. **Workflows beats a tutorial.** Mild-to-moderate jj/git users want recipes keyed to
   intent, not a linear story. Copy `gh stack`'s Typical Workflows shape — each
   workflow is a transcript with `#` comments.
2. **Glossary is one page.** `gt`'s per-term concept pages bloat the IA. One page, 5–8
   terms, link by name everywhere.
3. **Reference delegates to `--help`.** `gt`'s rule: CLI is source of truth for flags;
   site only carries the one-liner. We never diff two copies.
4. **No guides/marketing split.** One tree, one audience.

### 4b. Quick start (new)

Exactly five steps, one command each, no branching, no prose detours:

1. `jj-review doctor` — verify setup.
2. `jj-review submit @` — push the stack, open PRs.
3. Address review. Amend with `jj describe` / `jj split` as normal.
4. `jj-review submit @` — re-submit. Same command, idempotent.
5. `jj-review land @` — land what's ready.

Everything else is one link away.

### 4c. ASCII stack diagram (reused)

One diagram in `concepts.md`, reused at the top of every `workflows/*.md`:

```
@                 feat/top      PR #3   (draft)
│
○ change_id: xyz  feat/middle   PR #2
│
○ change_id: abc  feat/bottom   PR #1   (ready)
│
◉ trunk()         main
```

`change_id` primary (per AGENTS.md), `commit_id` elided for brevity, `feat/*` as the
review branches jj-review manages. Same diagram everywhere — users recognize it
instantly.

### 4d. The jj round-trip page (gap neither competitor covers)

Add `workflows/respond-to-review.md` covering specifically:

- What happens to change IDs on amend vs. on rebase (stable on amend — the whole point;
  a gh-stack user wouldn't know this).
- What happens when a reviewer pushes a suggestion from GitHub's UI into the review
  branch (we currently do not pull this back — document it; add to `backlog.md` if we
  intend to fix).
- What happens if a PR gets squash-merged on GitHub (mirrors `gh stack`'s `--onto`
  dance; we delegate to jj's rebase, but users need the full loop written out).
- What happens if the user manually pushes to a `review/…` branch (fails closed — name
  the error they'll see and the fix).

This is the page a jj-native tool has standing to write and a generic stacking tool
cannot.

## 5. Exceptional cases — the systematic list

Best-in-class means every situation has a named, discoverable response. Audit each
against: _named command? error names the fix? troubleshooting entry? exit code?_

| Situation                          | Named command         | Error names fix? | Troubleshooting | Exit |
| ---------------------------------- | --------------------- | ---------------- | --------------- | ---- |
| GitHub auth missing/expired        | `doctor`              | Yes ✓            | Yes ✓           | 3    |
| Trunk cannot be determined         | `doctor`              | Partial          | Add             | 3    |
| Stack not linear / divergent       | —                     | Yes ✓            | Yes ✓           | 5    |
| Ambiguous linkage (fails closed)   | `relink` / `unlink`   | Add              | Yes ✓           | 4    |
| Merge-base moved (trunk advanced)  | `cleanup --restack`   | Partial          | **Gap — add**   | —    |
| Bottom PR squash-merged on GitHub  | `cleanup --restack`   | **Gap**          | **Gap — add**   | —    |
| Mid-stack PR closed on GitHub      | `close` / `relink`    | **Gap**          | **Gap — add**   | —    |
| Interrupted jj-review op           | `abort`               | Yes ✓            | Yes ✓           | 6    |
| User pushed to `review/*` manually | —                     | **Gap — fail closed with named error** | **Gap — add** | 5 |
| PR exists, no local tracking       | `import`              | Yes ✓            | Yes ✓           | —    |
| Conflicts during restack           | defer to `jj`'s conflict UX | Partial    | **Gap — add one page** | — |
| Wrong PR linked                    | `relink`              | Add              | Add             | 4    |
| Abandon a change mid-stack         | `close` / `unlink`    | Add              | Add             | —    |

Filling the Gap cells is the bulk of the `troubleshooting.md` expansion. Each entry is
symptom → cause → remedy, three lines, following the existing pattern.

## 6. What to explicitly not do

- **Don't adopt `gt`'s force-push euphemism.** Be explicit that `submit` force-pushes
  `review/*` branches with lease; link the jj op log as the recovery path.
- **Don't split marketing from reference.** One docs tree.
- **Don't build a dashboard-shaped hole.** Every workflow terminates in the CLI or in
  GitHub's web UI. No "go open jj-review.dev".
- **Don't proliferate flags for mode changes.** If a command needs a "safe-auto" and a
  "touch-the-conflict" mode, they should be two commands (following `gh stack`'s
  `sync`/`rebase` split), not one command with `--interactive`.

## 7. Sequencing and enforcement

Priority order (each step independently shippable):

1. **Help-text template + short-help lint.** Test that (a) every command's short help
   starts with a verb and ends with a period, (b) every long help contains the six
   template sections in order. Prevents drift forever.
2. **Inline examples in `--help`.** Six commands, 2–3 examples each, drawn verbatim
   from `daily-workflow.md`.
3. **Semantic exit codes + `exit-codes.md`.** Mechanical, widely useful, cheap.
4. **Reshape `docs/` to the IA above.** Mostly plumbing — move, rename, split.
5. **Fill troubleshooting gaps from §5.** Highest-user-value page in the tree.
6. **`workflows/respond-to-review.md`** — the jj round-trip page. Unique value.
7. **`jj-review docs <topic>`** — small feature, large discoverability payoff.
8. **ASCII diagram + reuse across pages.**

Enforcement artifacts worth owning:

- `tests/unit/test_help_text.py` walking the argparse tree and asserting the template.
- A vocabulary linter over `docs/` (extend what AGENTS.md requires informally).
- A short rule in `docs/AGENTS.md` pinning the per-command `--help` template so future
  contributors don't drift.

## 8. Sources

Research conducted 2026-04-16.

- Graphite docs: https://graphite.com/docs, https://graphite.com/docs/command-reference,
  https://graphite.com/docs/cli-quick-start, https://graphite.com/docs/restack-branches,
  https://graphite.com/docs/track-branches.
- `withgraphite/docs` repo (flag-level detail for `gt` commands).
- `gh stack`: https://github.github.com/gh-stack/ (Overview, Quick Start, CLI
  reference, Typical Workflows, FAQ).
