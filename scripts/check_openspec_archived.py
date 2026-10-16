"""Fail when an OpenSpec change has all tasks complete but was not archived.

Reads ``openspec list --json`` output from stdin. Fails when a change in
``openspec/changes/`` has every task checked: such a change was forgotten in
the archive step, so its deltas never reached ``openspec/specs/``. A change
with an open task — work in progress or waiting for post-merge acceptance —
passes the check, but is listed in the step summary so lingering changes
stay visible.

    npx --yes @fission-ai/openspec@1.14.1 list --json \
        | python3 scripts/check_openspec_archived.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def main() -> int:
    changes: list[dict[str, Any]] = json.load(sys.stdin)["changes"]
    done = [
        c
        for c in changes
        if c["totalTasks"] > 0 and c["completedTasks"] == c["totalTasks"]
    ]
    open_ = [c for c in changes if c not in done]

    lines = ["## Open OpenSpec changes", ""]
    if open_:
        lines += ["| Change | Tasks |", "| --- | --- |"]
        lines += [
            f"| `{c['name']}` | {c['completedTasks']}/{c['totalTasks']} |"
            for c in open_
        ]
    else:
        lines.append("None.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))

    for c in done:
        print(
            f"::error::change {c['name']}: all tasks complete but the change "
            f"is not archived — run `openspec archive {c['name']}` in this PR"
        )
    return 1 if done else 0


if __name__ == "__main__":
    sys.exit(main())
