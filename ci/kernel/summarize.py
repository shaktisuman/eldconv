"""Markdown for the job summary: how eld's kernel outputs differ from GNU ld's.

    python3 ci/kernel/summarize.py eldconv-check.json
"""
import json
import sys
from pathlib import Path

r = json.loads(Path(sys.argv[1]).read_text())
ref = r.get("reference") or {}
print(f"\n**check:** {'passed' if r['ok'] else 'failed'}; {r.get('invocations', 0)} link(s) through eld")
if ref:
    print(f"\n**compared with GNU ld:** {ref.get('compared', 0)} output(s)"
          + ("" if ref.get("returncode") == 0 else f" (reference build failed, rc={ref.get('returncode')})"))
rows = []
for o in r.get("outputs", []):
    name = Path(o.get("output") or "?").name
    for f in o.get("validation", []):
        rows.append((name, f["level"], "valid ELF: " + f["what"], f["message"]))
    for f in o.get("differences", []):
        rows.append((name, f["level"], f["what"], f["message"]))
if rows:
    print("\n| output | level | what | difference |\n|---|---|---|---|")
    for name, level, what, msg in rows:
        print(f"| `{name}` | {level} | {what} | {msg.replace('|', '/')} |")
