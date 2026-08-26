"""Default pull request text derived from jj change descriptions."""

from jj_stack.commands.submit.default_pr_text import default_pr_body


def test_default_body_unwraps_only_markdown_soft_line_breaks() -> None:
    description = """submit: describe a change

A prose paragraph that was
wrapped while composing it.

- A list item that was
  wrapped onto another line.
- A second item.

> A quote that was
> wrapped too.

An unmatched *marker still
unwraps without a parse error.

A deliberate hard break.\\
This stays separate.

| Name | Value |
| --- | --- |
| one | two |

```text
wrapped-looking
code stays
```
"""

    expected = """A prose paragraph that was wrapped while composing it.

- A list item that was wrapped onto another line.
- A second item.

> A quote that was wrapped too.

An unmatched *marker still unwraps without a parse error.

A deliberate hard break.\\
This stays separate.

| Name | Value |
| --- | --- |
| one | two |

```text
wrapped-looking
code stays
```"""

    assert default_pr_body(description, template="unused") == expected
