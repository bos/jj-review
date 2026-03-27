"""Show how the selected jj stack currently appears on GitHub.

This reports the pull requests and GitHub branches jj-review is using for each
change without changing anything.
"""

from __future__ import annotations

from jj_review import review_inspection as _review_inspection

for _name in dir(_review_inspection):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_review_inspection, _name)

del _name
