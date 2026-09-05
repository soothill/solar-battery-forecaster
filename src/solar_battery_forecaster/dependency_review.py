from __future__ import annotations

import json
import sys
from typing import Any


def vulnerable_changes(payload: Any) -> int:
    pages = payload if isinstance(payload, list) else None
    if pages is None:
        raise ValueError("dependency comparison was not a list")
    if pages and all(isinstance(page, list) for page in pages):
        changes = [change for page in pages for change in page]
    else:
        changes = pages
    if any(not isinstance(change, dict) for change in changes):
        raise ValueError("dependency comparison contained an invalid change")
    total = 0
    for change in changes:
        vulnerabilities = change.get("vulnerabilities", [])
        if not isinstance(vulnerabilities, list):
            raise ValueError("dependency comparison contained invalid vulnerabilities")
        total += len(vulnerabilities)
    return total


def main() -> None:
    try:
        count = vulnerable_changes(json.load(sys.stdin))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("dependency comparison response was invalid") from exc
    if count:
        raise SystemExit("dependency comparison reported a vulnerable change")


if __name__ == "__main__":
    main()
