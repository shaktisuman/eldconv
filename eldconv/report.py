"""Readable reports of a check: for the console, and as Markdown next to eldconv-check.json.

Both are built from the check's JSON (CheckResult.as_dict), so a report can be rendered again
from a saved eldconv-check.json. The console shows what needs attention -- errors and
warnings, per output -- and counts the rest; the Markdown file has everything, including
each compared output's segment and section layout next to the reference's.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

LEVELS = ("error", "warn", "info")
_COLOR = {"error": "31", "warn": "33", "info": "2", "ok": "32", "bold": "1", "dim": "2"}
_LABEL = {"error": "error", "warn": "warning", "info": "info"}


class Paint:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, what: str) -> str:
        return f"\033[{_COLOR[what]}m{text}\033[0m" if self.enabled else text


def use_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _rel(path: Optional[str], root: Optional[Path]) -> str:
    if not path:
        return "?"
    if root is not None:
        try:
            return str(Path(path).relative_to(root))
        except ValueError:
            pass
    return path


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _counts(findings: Iterable[dict]) -> Dict[str, int]:
    c = {lv: 0 for lv in LEVELS}
    for f in findings:
        c[f.get("level", "info")] = c.get(f.get("level", "info"), 0) + 1
    return c


def _count_text(c: Dict[str, int], with_info: bool = True) -> str:
    parts = []
    if c["error"]:
        parts.append(_plural(c["error"], "error"))
    if c["warn"]:
        parts.append(_plural(c["warn"], "warning"))
    if with_info and c["info"]:
        parts.append(f"{c['info']} info")
    return ", ".join(parts) or "no differences"


def _findings(o: dict) -> List[dict]:
    """An output's findings: its validation, then its differences from the reference. A
    difference says more than the validation finding about the same thing (a missing
    PT_GNU_STACK, say), so that one is left out."""
    diffs = list(o.get("differences", []))
    said = {f["what"] for f in diffs if f["level"] != "info"}
    return ([dict(f, what="image: " + f["what"]) for f in o.get("validation", [])
             if f["what"] not in said] + diffs)


def _outputs(r: dict) -> List[dict]:
    return [o for o in r.get("outputs", []) if not o.get("trycompile") and o.get("output")]


def _general(items: List[str], outputs: List[dict]) -> List[str]:
    """Problems/warnings that are not about one output (those are listed under the output)."""
    names = tuple(f"{o['output']}: " for o in outputs if o.get("validation") or o.get("differences"))
    return [i for i in items if not (names and i.startswith(names))]


def _headline(r: dict) -> Tuple[str, str]:
    ref = r.get("reference") or {}
    bits = [r.get("eld_version") or r.get("eld") or "eld", _plural(r.get("invocations", 0), "link")]
    if ref.get("returncode") == 0:
        bits.append(f"{_plural(ref.get('compared', 0), 'output')} compared with the original build")
    elif ref:
        bits.append("original build failed")
    else:
        bits.append("not compared with the original build")
    return ("PASSED" if r.get("ok") else "FAILED"), " · ".join(bits)


# ---------------------------------------------------------------------------- console

def check_text(r: dict, root: Optional[Path] = None, color: bool = False, verbose: bool = False,
               files: Iterable[str] = ()) -> str:
    """The check result for a terminal: a headline, a table of outputs, what is wrong with
    each, the remaining problems and warnings, and where the full reports are."""
    p = Paint(color)
    status, line = _headline(r)
    out = [p(f"eldconv check {status}", "ok" if r.get("ok") else "error") + "  " + p(line, "dim"), ""]
    outs = _outputs(r)
    if outs:
        width = min(max(len(_rel(o["output"], root)) for o in outs), 60)
        out.append(p(f"  {'output':<{width}}  {'type':<4}  {'image':<7}  compared with the original", "bold"))
        hidden = 0
        for o in outs:
            v, d = _counts(o.get("validation", [])), _counts(o.get("differences", []))
            quiet = not (v["error"] or v["warn"] or d["error"] or d["warn"])
            if quiet and not verbose and len(outs) > 20:
                hidden += 1
                continue
            image = ("missing" if not o.get("exists", True) else "not ELF" if not o.get("elf")
                     else "broken" if v["error"] else "ok")
            image = p(f"{image:<7}", "error" if image != "ok" else "ok")
            if "differences" in o:
                cmp = _count_text(d, with_info=False)
                cmp = p(cmp, "error" if d["error"] else "warn" if d["warn"] else "ok")
            elif o.get("elf_type") in ("EXEC", "DYN"):
                cmp = p("not compared", "dim")
            else:
                cmp = p("-", "dim")
            name = _rel(o["output"], root)
            out.append(f"  {name:<{width}}  {(o.get('elf_type') or '?'):<4}  {image}  {cmp}")
        if hidden:
            out.append(p(f"  ... and {hidden} more output(s) with nothing to report", "dim"))
        out.append("")

    info_hidden = 0
    for o in outs:
        found = _findings(o)
        shown = sorted((f for f in found if verbose or f["level"] != "info"),
                       key=lambda f: LEVELS.index(f["level"]))
        info_hidden += len(found) - len(shown)
        if not shown:
            continue
        out.append(p(_rel(o["output"], root), "bold"))
        for f in shown:
            out.append(_finding_line(p, f))
        out.append("")
    if info_hidden:
        out.append(p(f"{info_hidden} informational difference(s) not shown (-v shows them; "
                     "the Markdown report has them all)", "dim"))
        out.append("")

    problems = _general(r.get("problems", []), outs)
    warnings = _general(r.get("warnings", []), outs)
    if problems:
        out.append(p("Problems", "error"))
        out.extend(f"  x {x}" for x in problems)
        out.append("")
    if warnings:
        out.append(p("Warnings", "warn"))
        out.extend(f"  ! {x}" for x in warnings)
        out.append("")
    files = [f for f in files if f]
    if files:
        out.append(p("Reports", "bold"))
        out.extend(f"  {f}" for f in files)
        out.append("")
    out.append(p(f"CHECK {status}", "ok" if r.get("ok") else "error"))
    return "\n".join(out)


def _finding_line(p: Paint, f: dict) -> str:
    label = _LABEL[f["level"]]
    return f"  {p(f'{label:<7}', f['level'])} {f['what']:<20} {f['message']}"


def compare_text(ref: str, new: str, findings: List[dict], lay: Optional[dict], color: bool = False,
                 verbose: bool = False) -> str:
    """`eldconv compare`: the findings and the two layouts side by side."""
    p = Paint(color)
    c = _counts(findings)
    out = [p(f"{new}", "bold") + p(f"  compared with {ref}", "dim"), ""]
    shown = [f for f in findings if verbose or f["level"] != "info"]
    for f in shown:
        out.append(_finding_line(p, f))
    if len(shown) < len(findings):
        out.append(p(f"  {len(findings) - len(shown)} informational difference(s) not shown (-v)", "dim"))
    if shown or len(shown) < len(findings):
        out.append("")
    if lay:
        out.append(p("Segments", "bold"))
        out.extend("  " + row for row in _segment_rows(lay))
        out.append("")
    out.append(p(f"COMPARE: {_count_text(c, with_info=False)}", "error" if c["error"] else
                 "warn" if c["warn"] else "ok"))
    return "\n".join(out)


def _segment_rows(lay: dict) -> List[str]:
    """PT_LOAD and the other segments, reference and eld next to each other."""
    rs, ns = lay["reference"]["segments"], lay["eld"]["segments"]
    types: List[str] = []
    for s in rs + ns:
        if s["type"] not in types:
            types.append(s["type"])
    types.sort(key=lambda t: t != "LOAD")         # the loadable segments first
    rows = [f"{'reference':<44}  eld"]
    for t in types:
        a = [s for s in rs if s["type"] == t]
        b = [s for s in ns if s["type"] == t]
        for i in range(max(len(a), len(b))):
            left = _seg_cell(a[i]) if i < len(a) else f"{t:<12} -"
            right = _seg_cell(b[i]) if i < len(b) else f"{t:<12} -"
            rows.append(f"{left:<44}  {right}")
    return rows


def _seg_cell(s: dict) -> str:
    return f"{s['type']:<12} {s['flags']:<3} {s['vaddr']:#014x} {s['memsz']:>#10x}"


# ---------------------------------------------------------------------------- markdown

def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


_MD_LEVEL = {"error": "🔴 error", "warn": "🟡 warning", "info": "⚪ info"}


def check_markdown(r: dict, root: Optional[Path] = None) -> str:
    status, line = _headline(r)
    out = [f"# eldconv check: {status}", "", line, ""]
    ref = r.get("reference") or {}
    rows = [("eld", r.get("eld") or "?"), ("eld version", r.get("eld_version") or "?"),
            ("build directory", r.get("build_dir") or "?"), ("build log", r.get("build_log") or "-"),
            ("links through eld", str(r.get("invocations", 0)))]
    if ref:
        rows += [("original build", ref.get("dir", "?")),
                 ("original build log", ref.get("build_log", "-")),
                 ("outputs compared", str(ref.get("compared", 0)))]
    out += ["| | |", "|---|---|"] + [f"| {k} | `{_md(v)}` |" for k, v in rows] + [""]

    outs = _outputs(r)
    if outs:
        out += ["## Outputs", "", "| output | type | image | compared with the original |",
                "|---|---|---|---|"]
        for o in outs:
            v, d = _counts(o.get("validation", [])), _counts(o.get("differences", []))
            image = ("missing" if not o.get("exists", True) else "not ELF" if not o.get("elf")
                     else "broken" if v["error"] else "ok")
            cmp = (_count_text(d) if "differences" in o
                   else "not compared" if o.get("elf_type") in ("EXEC", "DYN") else "-")
            script = " (linker script)" if o.get("script") else ""
            out.append(f"| `{_md(_rel(o['output'], root))}` | {o.get('elf_type') or '?'}{script} | "
                       f"{image} | {cmp} |")
        out.append("")

    problems = _general(r.get("problems", []), outs)
    warnings = _general(r.get("warnings", []), outs)
    if problems or warnings:
        out += ["## Problems and warnings", ""]
        out += [f"- 🔴 {_md(x)}" for x in problems] + [f"- 🟡 {_md(x)}" for x in warnings] + [""]

    for o in outs:
        found = _findings(o)
        if not found and not o.get("layout"):
            continue
        out += [f"## `{_md(_rel(o['output'], root))}`", ""]
        if o.get("reference"):
            out += [f"Compared with `{_md(o['reference'])}`"
                    + (", placed by a linker script." if o.get("script") else "."), ""]
        if found:
            out += ["| level | what | finding |", "|---|---|---|"]
            for f in sorted(found, key=lambda f: LEVELS.index(f["level"])):
                out.append(f"| {_MD_LEVEL[f['level']]} | {_md(f['what'])} | {_md(f['message'])} |")
            out.append("")
        if o.get("layout"):
            out += _layout_markdown(o["layout"])
    return "\n".join(out).rstrip() + "\n"


def compare_markdown(ref: str, new: str, findings: List[dict], lay: Optional[dict]) -> str:
    c = _counts(findings)
    out = [f"# eldconv compare: {_count_text(c)}", "", f"- reference: `{_md(ref)}`",
           f"- output: `{_md(new)}`", ""]
    if findings:
        out += ["| level | what | finding |", "|---|---|---|"]
        for f in sorted(findings, key=lambda f: LEVELS.index(f["level"])):
            out.append(f"| {_MD_LEVEL[f['level']]} | {_md(f['what'])} | {_md(f['message'])} |")
        out.append("")
    if lay:
        out += _layout_markdown(lay, open_=True)
    return "\n".join(out).rstrip() + "\n"


def _layout_markdown(lay: dict, open_: bool = False) -> List[str]:
    out = [f"<details{' open' if open_ else ''}><summary>Layout: segments and sections</summary>", ""]
    for side in ("reference", "eld"):
        out += [f"**Segments, {side}**", "",
                "| # | type | flags | vaddr | paddr | memsz | align | sections |",
                "|---|---|---|---|---|---|---|---|"]
        for i, s in enumerate(lay[side]["segments"]):
            secs = " ".join(s["sections"][:12]) + (" …" if len(s["sections"]) > 12 else "")
            out.append(f"| {i} | {s['type']} | {s['flags']} | `{s['vaddr']:#x}` | `{s['paddr']:#x}` | "
                       f"`{s['memsz']:#x}` | `{s['align']:#x}` | {_md(secs)} |")
        out.append("")
    rsec = {s["name"]: s for s in lay["reference"]["sections"]}
    nsec = {s["name"]: s for s in lay["eld"]["sections"]}
    names = [s["name"] for s in lay["eld"]["sections"]] + [n for n in rsec if n not in nsec]
    out += ["**Sections**", "",
            "| section | reference addr | size | loaded | eld addr | size | loaded | size change |",
            "|---|---|---|---|---|---|---|---|"]
    for n in names:
        a, b = rsec.get(n), nsec.get(n)
        ra = f"`{a['addr']:#x}` | {a['size']} | {a['segment'] or '-'}" if a else "- | - | -"
        nb = f"`{b['addr']:#x}` | {b['size']} | {b['segment'] or '-'}" if b else "- | - | -"
        delta = (f"{b['size'] - a['size']:+d}" if a and b and a["size"] != b["size"]
                 else "missing" if a and not b else "new" if b and not a else "")
        out.append(f"| `{_md(n)}` | {ra} | {nb} | {delta} |")
    out += ["", "</details>", ""]
    return out
