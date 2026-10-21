"""Turn the last line of a pytest run into a shields.io endpoint file (tests.json).

    python ci/test_badge.py pytest.out > tests.json
"""
import json
import re
import sys
from pathlib import Path

COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


def badge(text: str) -> dict:
    summary = ""
    for line in text.splitlines():
        # "=== 1117 passed, 3 skipped in 80.12s ===", or without the rule under -q
        if re.search(r" in [\d.]+s\b", line) and COUNT.search(line):
            summary = line
    n = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for num, word in COUNT.findall(summary):
        n["errors" if word.startswith("error") else word] = n.get(word, 0) + int(num)
    if not summary or not n["passed"] or n["failed"] or n["errors"]:
        bad = n["failed"] + n["errors"]
        message = f"{n['passed']} passed, {bad} failing" if summary else "no result"
        return {"schemaVersion": 1, "label": "tests", "message": message, "color": "red"}
    message = f"{n['passed']} passed" + (f", {n['skipped']} skipped" if n["skipped"] else "")
    return {"schemaVersion": 1, "label": "tests", "message": message, "color": "brightgreen"}


if __name__ == "__main__":
    try:
        text = Path(sys.argv[1]).read_text(errors="replace")
    except OSError:
        text = ""
    print(json.dumps(badge(text)))
