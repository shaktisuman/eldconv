"""Meson (meson.build, meson.options, machine files) rewriting.

Meson keeps linker flags in single-quoted string lists:

    add_project_link_arguments('-fuse-ld=lld', '-Wl,--icf=all', language: 'c')
    executable('app', 'main.c', link_args: ['-Wl,-O1', '-Wl,--gc-sections'])

and picks the linker in machine files (`[binaries] c_ld = 'lld'`) or through
the CC_LD/CXX_LD environment variables. Each string is converted on its own;
strings that convert to nothing leave the list.

eld is selected by injecting `-B<shim>/` with add_project_link_arguments()
right after every project() call -- the meson equivalent of reassigning LD in a
Makefile. That works for GCC and Clang alike (GCC cannot take a path in
-fuse-ld=, which is all `c_ld` can do), for native and cross builds, and in
subprojects, which each have their own project().
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..mapping import Diag
from ..rewrite import Rewriter
from .gn import _convert_gn_string as _convert_prefix_aware

MESON_STRING_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")
MACHINE_SECTION_RE = re.compile(r"^\s*\[(binaries|built-in options|properties|host_machine|"
                                r"target_machine|build_machine|constants|project options)\]", re.M)
MACHINE_LD_RE = re.compile(r"^(\s*)((?:c|cpp|objc|objcpp|fortran|d|rust|cuda)_ld)\s*=.*$")
PROJECT_CALL_RE = re.compile(r"^\s*project\s*\(", re.M)
INJECT_MARK = "# eldconv: link with eld"
C_FAMILY = ("c", "cpp", "objc", "objcpp", "fortran")


def is_machine_file(text: str) -> bool:
    return bool(MACHINE_SECTION_RE.search(text))


def _strip_empty_elements(ln: str) -> str:
    """Remove '' list elements left behind by conversion."""
    ln = re.sub(r"''\s*,\s*", "", ln)
    ln = re.sub(r",\s*''(?=\s*[\])])", "", ln)
    ln = re.sub(r"\[\s*''\s*\]", "[]", ln)
    ln = re.sub(r"\(\s*''\s*\)", "()", ln)
    return ln


def rewrite_meson_text(text: str, linker: str, file: str = "meson.build") -> Tuple[str, List[Diag]]:
    """Convert a meson.build / meson.options / machine file."""
    rw = Rewriter(linker, bare_requires_context=False)
    machine = is_machine_file(text)
    out: List[str] = []
    for i, ln in enumerate(text.split("\n"), 1):
        if machine:
            m = MACHINE_LD_RE.match(ln)
            if m:
                out.append(f"{m.group(1)}# eldconv: removed -> {ln.strip()}")
                rw.diags.append(Diag("warn", m.group(2), "removed; eld is selected via the -B shim", file, i))
                continue
        if "'" not in ln or ln.lstrip().startswith("#"):
            out.append(ln)
            continue
        removed: List[str] = []

        def repl(sm: re.Match) -> str:
            body = sm.group(1)
            if not body.strip() or "-" not in body:
                return sm.group(0)
            # '-fuse-ld=' + x: a prefix string is converted as if its value were there
            new = _convert_prefix_aware(rw, body, file, i)
            if new.strip() == body.strip():
                return sm.group(0)
            if not new.strip():
                removed.append(body)
                return "''"
            return "'" + new + "'"

        new_ln = MESON_STRING_RE.sub(repl, ln)
        if removed:
            if re.search(r"''\s*\+|\+\s*''", new_ln):
                rw.diags.append(Diag("warn", removed[0], "flag built by concatenation; the whole "
                                     "statement was commented out", file, i))
                out.append(f"{ln[:len(ln) - len(ln.lstrip())]}# eldconv: removed -> {ln.strip()}")
                continue
            new_ln = _strip_empty_elements(new_ln)
            if re.fullmatch(r"\s*''\s*,?\s*", new_ln):
                continue
        out.append(new_ln)
    return "\n".join(out), rw.diags


def _project_call_end(text: str, start: int) -> Optional[int]:
    """Index just past the `)` that closes the project( call starting at `start`."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            if text.startswith("'''", i):
                j = text.find("'''", i + 3)
                i = n if j < 0 else j + 3
                continue
            i += 1
            while i < n and text[i] != "'":
                i += 2 if text[i] == "\\" else 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def project_languages(text: str) -> List[str]:
    """Languages a meson project enables: project('x', 'c', 'cpp') + add_languages()."""
    langs: List[str] = []
    m = PROJECT_CALL_RE.search(text)
    if m:
        end = _project_call_end(text, m.end() - 1) or len(text)
        call = text[m.end():end]
        # positional strings before the first keyword argument; the first is the name
        head = re.split(r"\b\w+\s*:", call, maxsplit=1)[0]
        langs += MESON_STRING_RE.findall(head)[1:]
        lm = re.search(r"\blanguage\s*:\s*(\[[^\]]*\]|'[^']*')", call)
        if lm:
            langs += MESON_STRING_RE.findall(lm.group(1))
    for am in re.finditer(r"\badd_languages\s*\(([^)]*)\)", text):
        langs += MESON_STRING_RE.findall(re.split(r"\b\w+\s*:", am.group(1), maxsplit=1)[0])
    seen: List[str] = []
    for lang in (x.lower() for x in langs):
        lang = {"c++": "cpp", "objective-c": "objc", "objective-c++": "objcpp"}.get(lang, lang)
        if lang in C_FAMILY and lang not in seen:
            seen.append(lang)
    return seen


def inject_selection(text: str, shim_dir: str, file: str = "meson.build") -> Tuple[str, List[Diag]]:
    """Add the -B<shim>/ link argument right after the project() call."""
    if INJECT_MARK in text:
        return text, []
    m = PROJECT_CALL_RE.search(text)
    if not m:
        return text, []
    end = _project_call_end(text, m.end() - 1)
    if end is None:
        return text, [Diag("error", "project()", "could not find the end of the project() call; "
                           "eld is NOT selected for this project -- add "
                           f"`add_project_link_arguments('-B{shim_dir}/', language: ...)` by hand",
                           file, text.count("\n", 0, m.start()) + 1)]
    langs = project_languages(text)
    if not langs:
        return text, [Diag("warn", "project()", "no C-family language enabled here; nothing to "
                           "select eld for", file, text.count("\n", 0, m.start()) + 1)]
    block = f"""

{INJECT_MARK} (generated by eldconv; selects the eld shim for every link)
_eld_machines = [false]
if meson.is_cross_build()
  _eld_machines += [true]
endif
foreach _eld_lang : {langs!r}
  foreach _eld_native : _eld_machines
    _eld_have = true
    if _eld_native
      _eld_have = add_languages(_eld_lang, native: true, required: false)
    endif
    if _eld_have
      _eld_cc = meson.get_compiler(_eld_lang, native: _eld_native)
      _eld_args = ['-B{shim_dir}/']
      if _eld_cc.get_id() == 'gcc'
        # keep collect2 from handing eld GCC's LTO plugin (the shim strips it too)
        _eld_args += ['-fno-use-linker-plugin']
        if get_option('b_lto')
          # eld cannot use GCC's LTO plugin: compile fat LTO objects so they still link
          add_project_arguments('-ffat-lto-objects', language: _eld_lang, native: _eld_native)
        endif
      endif
      add_project_link_arguments(_eld_args, language: _eld_lang, native: _eld_native)
    endif
  endforeach
endforeach
"""
    line = text.count("\n", 0, end) + 1
    return text[:end] + block + text[end:], [Diag("info", "project()",
                                                  "eld shim selected after project()", file, line)]


BUILD_SH_MESON = """#!/bin/sh
# Generated by eldconv -- configure & build a Meson tree with eld.
# Extra arguments go to `meson setup` (e.g. --cross-file, -Dbuildtype=release).
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="${BUILD:-$SRC/build-eld}"
ELD="${ELDCONV_ELD:-@ELD@}"
export ELDCONV_ELD="$ELD"
# Meson picks a linker from these too; eld is selected by the project itself.
unset CC_LD CXX_LD OBJC_LD OBJCXX_LD FC_LD
# `meson setup` refuses a directory it has configured before
if [ -d "$BUILD/meson-private" ]; then
  meson setup --reconfigure "$BUILD" "$SRC" "$@"
else
  meson setup "$BUILD" "$SRC" "$@"
fi
meson compile -C "$BUILD"
"""
