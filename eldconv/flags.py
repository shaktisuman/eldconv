"""Tokenise linker options out of driver strings and re-emit them.

Handles: -Wl,a,b,c  |  -Xlinker a  |  CMake LINKER: / LINKER:SHELL:  |  raw ld argv.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List, Optional, Sequence

# Options whose value is the NEXT argument (GNU ld style).  Long options also
# accept "=value"; that form is handled generically.
VALUE_FLAGS = frozenset({
    "-o", "-e", "--entry", "-T", "--script", "-dT", "--dT", "-L", "-l", "-m", "-z", "-u", "--undefined",
    "-Map", "--Map", "--wrap", "--defsym", "--version-script", "--dynamic-linker", "-dynamic-linker",
    "-rpath", "--rpath", "-rpath-link", "--rpath-link", "--sort-section", "--exclude-libs",
    "-h", "-soname", "--soname", "--plugin", "--plugin-config", "--plugin-opt",
    "-plugin", "-plugin-opt", "--hash-style", "--image-base", "--section-start",
    "-y", "--trace-symbol", "--unresolved-symbols", "--thread-count", "--sysroot",
    "--lto-options", "--trampoline-map", "--symdef-file", "-b", "--format", "--oformat",
    # value-taking options that the mapping table drops: listed so the value is
    # consumed with the flag instead of being left behind as an input file.
    "--section-ordering-file", "--call-graph-ordering-file", "--split-stack-adjust-size",
    "--icf-iterations", "--mllvm", "-mllvm", "--chroot", "--dependency-file",
    "--time-trace-file", "--thinlto-cache-policy", "--lto-partitions", "--lto-sample-profile",
    "--lto-basic-block-sections", "--lto-obj-path", "--compress-sections", "--why-extract",
    "--package-metadata", "--spare-dynamic-tags", "--spare-program-headers",
    "--incremental-base", "--warn-backrefs-exclude", "--print-archive-stats",
    "-R", "--just-symbols", "--retain-symbols-file", "--dynamic-list",
    "--export-dynamic-symbol", "--symbol-ordering-file", "-O", "--icf",
    "--pack-dyn-relocs", "--thinlto-jobs", "--thinlto-cache-dir", "--compress-debug-sections",
    "--orphan-handling", "-Ttext", "-Tdata", "-Tbss", "--error-limit", "--build-id",
    "--reproduce", "--Map", "--gc-cref", "--copy-farcalls-from-file",
})



def _harvested_value_options() -> frozenset:
    """Options some source linker documents as taking a separate argument.

    Dropping such an option without its argument would leave the argument behind
    as a bogus input file, so they are grouped even when eldconv has no rule.
    `--threads` and `--unique` only take a value via `=` in bfd/gold, where a
    following token is an input file, so they are left out.
    """
    import json
    from pathlib import Path
    try:
        d = json.loads((Path(__file__).parent / "data" / "source_options.json").read_text())
    except (OSError, ValueError):
        return frozenset()
    return frozenset(d.get("value_options", [])) - {"--threads", "--unique", "--run"}


VALUE_FLAGS = VALUE_FLAGS | _harvested_value_options()

# Short options that may carry their value attached: -lfoo -L/x -O2 -znow -melf
ATTACHED_SHORT = ("-l", "-L", "-O", "-z", "-m")

# Options whose value legitimately starts with a dash (`-Wl,-mllvm,-inline-threshold=0`).
# For every other option a following token that looks like an option is NOT eaten as
# its value, so `--incremental-base --incremental` stays two options.
DASH_VALUE_OK = frozenset({"--mllvm", "-mllvm", "--plugin-opt", "-plugin-opt", "--lto-options"})

# Long options that never take a value even though "=" might appear in odd spellings.
NO_VALUE_ON_NEXT = frozenset({"--build-id", "--reproduce"})  # value only via "="


@dataclass
class LinkArg:
    """One linker option (possibly with value)."""
    name: str
    value: Optional[str] = None
    sep: str = ""          # "=" | "," | " " | ""  ("" == attached, e.g. -lfoo)
    positional: bool = False

    def parts(self) -> List[str]:
        """Return the option as a list of argv-level tokens (sep-aware)."""
        if self.value is None:
            return [self.name]
        if self.sep in ("=", ""):
            return [f"{self.name}{self.sep}{self.value}"]
        return [self.name, self.value]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return " ".join(self.parts())


# --------------------------------------------------------------------------- parsing

def _group(parts: Sequence[str], value_sep: str) -> List[LinkArg]:
    """Group flat tokens into LinkArgs, attaching values to flags that take them."""
    out: List[LinkArg] = []
    i = 0
    n = len(parts)
    while i < n:
        p = parts[i]
        if p == "":
            i += 1
            continue
        if not p.startswith("-") or p == "-":
            out.append(LinkArg(p, positional=True))
            i += 1
            continue
        if p.startswith("--") and "=" in p:
            name, val = p.split("=", 1)
            out.append(LinkArg(name, val, "="))
            i += 1
            continue
        # single-dash long options with "=" (e.g. -Map=file, -Ttext=0x1000, -plugin-opt=x)
        if p.startswith("-") and not p.startswith("--") and "=" in p and len(p) > 3:
            name, val = p.split("=", 1)
            if name in VALUE_FLAGS:
                out.append(LinkArg(name, val, "="))
                i += 1
                continue
        if p in VALUE_FLAGS and p not in NO_VALUE_ON_NEXT and i + 1 < n:
            nxt = parts[i + 1]
            if not (len(nxt) > 1 and nxt.startswith("-") and p not in DASH_VALUE_OK):
                out.append(LinkArg(p, nxt, value_sep))
                i += 2
                continue
        if p.startswith(ATTACHED_SHORT) and len(p) > 2 and not p.startswith("--") and p[:2] in VALUE_FLAGS:
            out.append(LinkArg(p[:2], p[2:], ""))
            i += 1
            continue
        out.append(LinkArg(p))
        i += 1
    return out


def parse_wl(fragment: str) -> List[LinkArg]:
    """Parse a single `-Wl,...` token (or a CMake `LINKER:...` item body)."""
    if fragment.startswith("-Wl,"):
        fragment = fragment[4:]
    return _group(fragment.split(","), ",")


def parse_linker_item(item: str) -> List[LinkArg]:
    """Parse a CMake `LINKER:` item body (after the `LINKER:` prefix)."""
    if item.startswith("SHELL:"):
        return _group(shlex.split(item[len("SHELL:"):]), " ")
    return _group(item.split(","), ",")


def parse_raw(tokens: Sequence[str]) -> List[LinkArg]:
    """Parse a raw linker argv (no -Wl wrapping)."""
    return _group(list(tokens), " ")


def parse_xlinker(tokens: Sequence[str]) -> List[LinkArg]:
    """Parse the values of a run of `-Xlinker v` pairs."""
    return _group(list(tokens), " ")


# --------------------------------------------------------------------------- emission

def emit_wl(args: Sequence[LinkArg]) -> str:
    """Emit args as a single `-Wl,...` token ('' if nothing left)."""
    parts: List[str] = []
    for a in args:
        parts.extend(a.parts())
    if not parts:
        return ""
    return "-Wl," + ",".join(parts)


def emit_xlinker(args: Sequence[LinkArg]) -> str:
    parts: List[str] = []
    for a in args:
        parts.extend(a.parts())
    return " ".join(f"-Xlinker {p}" for p in parts)


def emit_linker_item(args: Sequence[LinkArg], shell: bool) -> str:
    parts: List[str] = []
    for a in args:
        parts.extend(a.parts())
    if not parts:
        return ""
    if shell:
        return "LINKER:SHELL:" + " ".join(shlex.quote(p) if " " in p else p for p in parts)
    return "LINKER:" + ",".join(parts)


def emit_raw(args: Sequence[LinkArg]) -> List[str]:
    parts: List[str] = []
    for a in args:
        parts.extend(a.parts())
    return parts


def split_driver(s: str) -> List[str]:
    """shlex-split a driver flag string, tolerating make/cmake variable refs."""
    return shlex.split(s, posix=True)
