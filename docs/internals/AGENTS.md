# Internal documentation guidance

These documents are for contributors and maintainers. The [index](README.md) identifies the
source for product rules, architecture, tests, reviews, and releases. Keep each rule in its owning
document and link to it elsewhere.

Use ordinary technical language. Project-specific terms are useful when they name a real type,
field, module, or enduring rule; define them at first use. Prefer concrete inputs, checks, and
effects to metaphors, new taxonomies, and unexplained shorthand.

Describe the current system. Keep implementation history in `jj` commits and task progress in the
conversation, not in these documents. Retain the reason for a non-obvious rule when a future
contributor would need it to avoid a mistake.

When code, tests, and docs disagree, check the supported workflow and intended rule before
editing. Do not turn an accidental implementation detail into product policy, or silently change
policy while correcting prose.

The [public vocabulary rules](../AGENTS.md#audience-and-vocabulary) apply to user-facing docs and
help; internal type names do not belong there merely because they appear here.
