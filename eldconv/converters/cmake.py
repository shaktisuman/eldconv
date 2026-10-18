"""CMake file rewriting on top of the generic flag rewriter."""
from __future__ import annotations

import re
from typing import List, Tuple

from ..mapping import Diag
from ..rewrite import Rewriter

SET_LINKER_TYPE_RE = re.compile(r"^(\s*)(set\s*\(\s*CMAKE_LINKER_TYPE\b.*\)\s*)$", re.I)
CACHE_LINKER_TYPE_RE = re.compile(r"^CMAKE_LINKER_TYPE(:\w+)?=.*$", re.I | re.M)
SET_LINKER_RE = re.compile(r"^(\s*)set\s*\(\s*CMAKE_LINKER\s+[^)]*\)", re.I)
LINKER_TYPE_PROP_RE = re.compile(r"\bLINKER_TYPE\s+\"?(LLD|GOLD|BFD|MOLD|DEFAULT|SYSTEM)\"?", re.I)
EMPTY_PROPS_RE = re.compile(r"PROPERTIES\s*\)")
CMAKE_C_COMPILER_LINKER_RE = re.compile(r"^(\s*)set\s*\(\s*CMAKE_(C|CXX|ASM)_COMPILER_LINKER\b[^)]*\)", re.I)


def rewrite_cmake_text(text: str, linker: str, file: str = "CMakeLists.txt") -> Tuple[str, List[Diag]]:
    rw = Rewriter(linker, strip_empty_quotes=True)
    out: List[str] = []
    for i, ln in enumerate(text.split("\n"), 1):
        m = SET_LINKER_TYPE_RE.match(ln)
        if m:
            out.append(f"{m.group(1)}# eldconv: removed -> {m.group(2).strip()}")
            rw.diags.append(Diag("warn", "CMAKE_LINKER_TYPE", "removed; eld selected via -B shim", file, i))
            continue
        m = SET_LINKER_RE.match(ln)
        if m:
            out.append(f'{m.group(1)}set(CMAKE_LINKER "${{ELD_SHIM_LD}}")  # eldconv: was {ln.strip()}')
            rw.diags.append(Diag("info", "CMAKE_LINKER", "pointed at eld shim", file, i))
            continue
        m = CMAKE_C_COMPILER_LINKER_RE.match(ln)
        if m:
            out.append(f'{m.group(1)}# eldconv: removed -> {ln.strip()}')
            rw.diags.append(Diag("warn", f"CMAKE_{m.group(2)}_COMPILER_LINKER", "removed; eld selected via -B shim", file, i))
            continue
        if LINKER_TYPE_PROP_RE.search(ln) and "CMAKE_LINKER_TYPE" not in ln:
            ln2 = re.sub(r"(?<=\S)[ \t]{2,}", " ", LINKER_TYPE_PROP_RE.sub("", ln))
            rw.diags.append(Diag("warn", "LINKER_TYPE", "target property removed; eld selected via -B shim", file, i))
            if EMPTY_PROPS_RE.search(ln2):
                out.append(f"{ln[:len(ln) - len(ln.lstrip())]}# eldconv: removed -> {ln.strip()}")
                continue
            ln = ln2
        out.append(rw.rewrite_line(ln, file, i))
    return "\n".join(out), rw.diags


_PRESET_DROP_VARS = ("CMAKE_LINKER_TYPE", "CMAKE_LINKER", "CMAKE_C_COMPILER_LINKER",
                     "CMAKE_CXX_COMPILER_LINKER")


def rewrite_cmake_presets_text(text: str, linker: str, file: str = "CMakePresets.json") -> Tuple[str, List[Diag]]:
    """CMakePresets.json / CMakeUserPresets.json.

    Parsed as JSON rather than rewritten as text: a linker selection here is a
    key of "cacheVariables" (`"CMAKE_LINKER_TYPE": "LLD"`), which has to be
    removed as a key/value pair, and flags live in string values (cache
    variables and the "environment" of a preset).
    """
    import json
    from ..rewrite import convert_driver_string
    try:
        doc = json.loads(text)
    except ValueError:
        rw = Rewriter(linker)
        return rw.rewrite_text(text, file), rw.diags
    diags: List[Diag] = []
    changed = False

    def conv(v: str) -> str:
        nonlocal changed
        new, d = convert_driver_string(v, linker)
        for x in d:
            x.file = file
        diags.extend(d)
        if new != v:
            changed = True
        return new

    def walk(node, path=()):
        nonlocal changed
        if isinstance(node, dict):
            for key in list(node):
                val = node[key]
                if path and path[-1] == "cacheVariables" and key in _PRESET_DROP_VARS:
                    del node[key]
                    changed = True
                    diags.append(Diag("warn", key, "removed from preset; eld is selected via the -B shim", file))
                    continue
                if path and path[-1] in ("cacheVariables", "environment"):
                    if isinstance(val, str) and "-" in val:
                        node[key] = conv(val)
                        continue
                    if isinstance(val, dict) and isinstance(val.get("value"), str):
                        val["value"] = conv(val["value"])
                        continue
                walk(val, path + (key,))
        elif isinstance(node, list):
            for x in node:
                walk(x, path)

    walk(doc)
    if not changed:
        return text, diags
    return json.dumps(doc, indent=2) + "\n", diags


def rewrite_cmake_cache_text(text: str, linker: str, file: str = "CMakeCache.txt") -> Tuple[str, List[Diag]]:
    """CMakeCache.txt is normally not copied; if it is, neutralise linker selection."""
    diags: List[Diag] = []
    text2 = CACHE_LINKER_TYPE_RE.sub("", text)
    if text2 != text:
        diags.append(Diag("warn", "CMAKE_LINKER_TYPE", "removed from cache", file, None))
    t3, d = rewrite_cmake_text(text2, linker, file)
    return t3, diags + d
