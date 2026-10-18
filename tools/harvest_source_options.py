#!/usr/bin/env python3
"""Regenerate eldconv/data/source_options.json: every option bfd, gold, lld and
mold accept, plus every `-z` keyword.

This is a maintainer tool, not part of eldconv's runtime. It reads:

  --bfd-help  FILE   output of `ld.bfd --help`
  --gold-help FILE   output of `ld.gold --help`
  --lld-help  FILE   output of `ld.lld --help`
  --mold-help FILE   output of `mold --help`
  --lld-src   DIR    an llvm-project/lld/ELF checkout (Options.td, Driver.cpp)
  --mold-doc  FILE   mold's docs/mold.md

Any input may be omitted; the matching set is then just smaller.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eldconv.checker import parse_help  # noqa: E402

# Options added to GNU ld after the 2.42 release most distros ship.
BFD_EXTRA = {"--section-ordering-file", "--rosegment", "--no-rosegment", "--error-execstack",
             "--no-error-execstack", "--error-rwx-segments", "--no-error-rwx-segments",
             "--warn-execstack-objects", "--remap-inputs", "--remap-inputs-file",
             "--package-metadata", "--enable-linker-version", "--disable-linker-version"}
BFD_Z_EXTRA = {"memory-seal", "nomemory-seal", "mark-plt", "nomark-plt", "sectionheader",
               "nosectionheader", "pack-relative-relocs", "nopack-relative-relocs",
               "isa-level-report", "start-stop-gc", "nostart-stop-gc", "unique-symbol",
               "nounique-symbol", "report-relative-reloc", "indirect-extern-access",
               "noindirect-extern-access", "execstack", "noexecstack"}
SKIP = {"-o", "--output", "--help", "-h", "--version", "-v", "-V", "--usage", "-(", "-)",
        "-z", "-l", "-L", "--no-"}
OPT_RE = re.compile(r"-{1,2}[A-Za-z(][\w.+()-]*")
Z_RE = re.compile(r"[a-z][a-z0-9-]*[a-z0-9]")


HEAD_VAL_RE = re.compile(r"(-{1,2}[A-Za-z][\w.+-]*)\s+(?:[A-Z][A-Z_]*\b|<[^>]+>)")


def value_opts_from_help(path):
    """Options shown taking a *separate* argument (`--oformat TARGET`, `-F <value>`)."""
    if not path:
        return set()
    out = set()
    for line in Path(path).read_text().splitlines():
        if not line.lstrip().startswith("-"):
            continue
        head = re.split(r"\s{2,}", line.strip())[0]
        out |= {m.group(1) for m in HEAD_VAL_RE.finditer(head)}
    return out


def from_help(path):
    if not path:
        return set(), set()
    txt = Path(path).read_text()
    return ({o for o in parse_help(txt) if len(o) > 1},
            set(re.findall(r"^\s*-z\s+([a-z][a-z0-9-]*)", txt, re.M)))


def from_lld_src(d):
    if not d:
        return set(), set(), set()
    td = (Path(d) / "Options.td").read_text()
    drv = (Path(d) / "Driver.cpp").read_text()
    opts = set()
    for m in re.finditer(r'\b(F|J|FF|JJ|Eq|EEq|B|BB)<"([^"]+)"', td):
        kind, name = m.groups()
        name = name.rstrip("=")
        opts.add("--" + name)
        if kind in ("B", "BB"):
            opts.add("--no-" + name)
    for m in re.finditer(r'(?:Flag|Joined|Separate|JoinedOrSeparate)<\[([^\]]*)\],\s*"([^"]+)"', td):
        prefixes, name = m.groups()
        opts.add(("--" if '"--"' in prefixes else "-") + name.rstrip("="))
    vals = {"--" + n.rstrip("=") for n in re.findall(r'\bE?Eq<"([^"]+)"', td)}
    for m in re.finditer(r'(?:Separate|JoinedOrSeparate)<\[([^\]]*)\],\s*"([^"]+)"', td):
        prefixes, name = m.groups()
        vals.add(("--" if '"--"' in prefixes else "-") + name)
    z = set()
    m = re.search(r"isKnownZFlag\(StringRef s\)\s*\{(.*?)\n\}", drv, re.S)
    if m:
        z |= set(re.findall(r'"([a-z0-9-]+)"', m.group(1)))
    z |= set(re.findall(r'hasZOption\(args, "([a-z0-9-]+)"\)', drv))
    for pair in re.findall(r'getZFlag\(args, "([a-z0-9-]+)", "([a-z0-9-]+)"', drv):
        z |= set(pair)
    return opts, z, vals


def from_mold_doc(path):
    if not path:
        return set(), set()
    md = Path(path).read_text()
    opts = set(re.findall(r"^\*\s+`(-{1,2}[A-Za-z][\w-]*)", md, re.M))
    opts |= set(re.findall(r"`(--[a-z][\w-]*)", md))
    return opts, set(re.findall(r"`-z ([a-z][a-z0-9-]*)", md))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for k in ("bfd", "gold", "lld", "mold"):
        p.add_argument(f"--{k}-help")
    p.add_argument("--lld-src")
    p.add_argument("--mold-doc")
    p.add_argument("-o", "--output", default=str(Path(__file__).resolve().parent.parent
                                                  / "eldconv" / "data" / "source_options.json"))
    a = p.parse_args(argv)
    sets = {k: from_help(getattr(a, f"{k}_help")) for k in ("bfd", "gold", "lld", "mold")}
    lo, lz, lv = from_lld_src(a.lld_src)
    mo, mz = from_mold_doc(a.mold_doc)
    opts = {"bfd": sets["bfd"][0] | BFD_EXTRA, "gold": sets["gold"][0],
            "lld": sets["lld"][0] | lo, "mold": sets["mold"][0] | mo}
    vals = lv.union(*(value_opts_from_help(getattr(a, f"{k}_help")) for k in ("bfd", "gold", "lld", "mold")))
    zs = {"bfd": sets["bfd"][1] | BFD_Z_EXTRA, "gold": sets["gold"][1],
          "lld": sets["lld"][1] | lz, "mold": sets["mold"][1] | mz}
    out = {"_comment": "Options each source linker accepts. Generated by "
                       "tools/harvest_source_options.py; do not edit by hand.",
           "options": {k: sorted(o for o in v if o not in SKIP and OPT_RE.fullmatch(o))
                       for k, v in opts.items()},
           "z": {k: sorted(z for z in v if Z_RE.fullmatch(z)) for k, v in zs.items()},
           # options that take their value as the NEXT argument in some spelling
           "value_options": sorted(o for o in vals if OPT_RE.fullmatch(o) and o not in SKIP)}
    Path(a.output).write_text(json.dumps(out, indent=1) + "\n")
    print({k: len(v) for k, v in out["options"].items()}, {k: len(v) for k, v in out["z"].items()})


if __name__ == "__main__":
    main()
