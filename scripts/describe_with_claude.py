#!/usr/bin/env python3
# Tested with Claude Code 2.1.81.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROMPT_TEMPLATE = """\
{task}

Return JSON with exactly two string fields:
- `title`: a one-line title
- `body`: GitHub-flavored Markdown

- Do not mention AI.
- Do not wrap the JSON in code fences.
- Keep the title concise, specific, and informative.
- Prefer reviewer-useful summaries over diff narration.
- Explain what changed, why it changed, risks, and testing when known.
{extra_guidance}

Use the source control context below:
{context}
"""

SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["title", "body"],
        "additionalProperties": False,
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate jj-review metadata with Claude Code. Prints JSON with "
            "string `title` and `body` fields."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="REVSET", help="Generate metadata for one PR revset.")
    group.add_argument(
        "--stack",
        metavar="REVSET",
        help="Generate metadata for one stack revset.",
    )
    return parser.parse_args()


def run_jj(*args: str) -> str:
    completed = subprocess.run(
        ["jj", *args],
        capture_output=True,
        check=False,
        cwd=Path.cwd(),
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "unknown jj failure"
        raise SystemExit(detail)
    return completed.stdout


def build_context(mode: str, revset: str) -> str:
    if mode == "pr":
        return run_jj("show", "--git", "-r", revset).strip()
    return run_jj(
        "log",
        "-p",
        "--git",
        "-r",
        f"trunk()::{revset} & visible() & mutable()",
        "--no-graph",
    ).strip()


def stack_commit_count(revset: str) -> int:
    return int(
        run_jj(
            "log",
            "--count",
            "-r",
            f"trunk()::{revset} & visible() & mutable()",
        ).strip()
    )


def build_prompt(mode: str, revset: str, context: str) -> str:
    if mode == "pr":
        task = "Write a GitHub pull request title and body for a human reviewer."
        extra_guidance = (
            "- Optimize for a reviewer who wants to understand one change quickly."
        )
    else:
        task = "Write a GitHub stack summary for a human reviewer."
        commit_count = stack_commit_count(revset)
        if commit_count == 1:
            extra_guidance = "\n".join(
                [
                    "- This stack contains exactly one commit.",
                    "- Describe that one change directly.",
                    "- Do not invent a broader series or mention multiple commits.",
                    "- The body will appear above an existing stack-navigation comment.",
                ]
            )
        else:
            extra_guidance = "\n".join(
                [
                    f"- This stack contains {commit_count} commits.",
                    "- Summarize the series as a whole, not just the top commit.",
                    "- Explain how the changes in the stack fit together.",
                    "- The body will appear above an existing stack-navigation comment.",
                ]
            )
    return PROMPT_TEMPLATE.format(
        context=context or "(no source control context available)",
        extra_guidance=extra_guidance,
        task=task,
    )


def parse_model_output(text: str) -> dict[str, str]:
    payload = json.loads(text)
    structured_output = payload.get("structured_output") if isinstance(payload, dict) else None
    if isinstance(structured_output, dict) and all(
        isinstance(structured_output.get(field), str) for field in ("title", "body")
    ):
        return {
            "title": structured_output["title"],
            "body": structured_output["body"],
        }
    if isinstance(payload, dict) and all(
        isinstance(payload.get(field), str) for field in ("title", "body")
    ):
        return {"title": payload["title"], "body": payload["body"]}
    for key in ("result", "message", "text"):
        candidate = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(candidate, str):
            continue
        nested = json.loads(candidate)
        if isinstance(nested, dict) and all(
            isinstance(nested.get(field), str) for field in ("title", "body")
        ):
            return {"title": nested["title"], "body": nested["body"]}
    raise SystemExit("Claude did not return a JSON object with string title/body fields.")


def main() -> int:
    args = parse_args()
    mode = "pr" if args.pr is not None else "stack"
    revset = args.pr if args.pr is not None else args.stack
    if revset is None:
        raise AssertionError("argparse should guarantee a revset.")

    prompt = build_prompt(mode, revset, build_context(mode, revset))
    claude_bin = os.environ.get("JJ_REVIEW_CLAUDE_BIN", "claude")
    completed = subprocess.run(
        [
            claude_bin,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            SCHEMA,
            prompt,
        ],
        capture_output=True,
        check=False,
        cwd=Path.cwd(),
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "Claude failed"
        print(detail, file=sys.stderr)
        return completed.returncode or 1

    print(json.dumps(parse_model_output(completed.stdout)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
