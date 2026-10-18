"""Makefile rewriting on top of the generic flag rewriter."""
from __future__ import annotations

import os
import re
from typing import List, Tuple

from .. import LINKERS, SHIM_DIR
from ..mapping import Diag
from ..rewrite import Rewriter

_LK = "|".join(LINKERS)
LD_ASSIGN_RE = re.compile(
    r"^(\s*(?:export\s+|override\s+)?LD\s*(?::{1,3}|[?+!])?=\s*)(.*?)(\s*(?:#.*)?)$")
LD_VALUE_IS_LINKER_RE = re.compile(r"(?:(?<![\w.])ld(?:\.(" + _LK + r"))?|(?<![\w.-])mold)(?![\w.])")
MOLD_RUN_RE = re.compile(r"(?<![\w-])mold\s+-run\s+")
# A recipe that runs a linker itself instead of going through $(LD) or the
# compiler driver: `\tld.gold -r -o x.o a.o b.o`, `\t$(CROSS_COMPILE)ld -r ...`.
# Only command positions count (start of the recipe, or after ; && || |).
RECIPE_LD_RE = re.compile(
    r"((?:^\t|;|&&|\|\||\|)[ \t]*[@+\-]*[ \t]*(?:\$\(Q\)[ \t]*)?)"
    r"((?:[^\s;&|()]*/)?(?:\$[({]\w+[)}])?(?:ld(?:\.(?:bfd|gold|lld))?|mold)(?:-[\d.]+)?)(?=[ \t]|$)")
INCLUDE_MARK = "# eldconv: link with eld"


def rewrite_make_text(text: str, linker: str, file: str = "Makefile", eld_mk_rel: str = "eld.mk",
                      add_include: bool = True, shell: bool = False) -> Tuple[str, List[Diag]]:
    """Convert one makefile.

    `add_include` appends the `include eld.mk` block. Only entry-point makefiles
    and ones that now reference $(ELD_SHIM_LD) need it -- see needs_include().
    """
    rw = Rewriter(linker, shell=shell)
    out: List[str] = []
    for i, ln in enumerate(text.split("\n"), 1):
        m = LD_ASSIGN_RE.match(ln)
        if m and LD_VALUE_IS_LINKER_RE.search(m.group(2)) and "ELD" not in m.group(2):
            # eld.mk (which defines ELD_SHIM_LD) is included at the end of the file, so
            # a use of LD earlier in the file (`X := $(shell $(LD) --version)`) would see
            # nothing: fall back to the shim's path, worked out from this makefile's location.
            shim_dir = os.path.dirname(eld_mk_rel)
            shim_dir = shim_dir + "/" if shim_dir else ""
            shim = "$(ELD_SHIM_LD)"
            if re.search(r":{1,3}=|!=", m.group(1)):    # simply expanded: evaluated right here
                shim = (f"$(if $(ELD_SHIM_LD),$(ELD_SHIM_LD),$(abspath "
                        f"$(dir $(firstword $(MAKEFILE_LIST))){shim_dir}{SHIM_DIR}/ld))")
            out.append(f"{m.group(1)}{shim}{m.group(3)}  # eldconv: was {m.group(2).strip()}")
            rw.diags.append(Diag("info", "LD", f"LD reassigned from `{m.group(2).strip()}` to eld shim", file, i))
            continue
        if MOLD_RUN_RE.search(ln):
            ln = MOLD_RUN_RE.sub("", ln)
            rw.diags.append(Diag("warn", "mold -run", "wrapper removed", file, i))
        if ln.startswith("\t") and RECIPE_LD_RE.search(ln):
            def _direct(m: re.Match, _i=i) -> str:
                rw.diags.append(Diag("info", m.group(2),
                                     "recipe runs the linker directly; now runs eld's shim", file, _i))
                return m.group(1) + "$(ELD_SHIM_LD)"
            ln = RECIPE_LD_RE.sub(_direct, ln)
        out.append(rw.rewrite_line(ln, file, i))
    new = "\n".join(out)
    if add_include and INCLUDE_MARK not in new:
        if not new.endswith("\n"):
            new += "\n"
        new += f"\n{INCLUDE_MARK}\n{include_block(eld_mk_rel)}"
    return new, rw.diags


def include_block(eld_mk_rel: str) -> str:
    """The `include eld.mk` snippet, anchored to make's entry-point makefile.

    `$(lastword $(MAKEFILE_LIST))` cannot be used: it stops naming the current
    file as soon as that file includes another one, so the path would be
    resolved against some unrelated directory. `$(firstword ...)` is the
    makefile make was started on, which is stable for the whole run. When this
    file is *not* the entry point the variables are already inherited from the
    top-level make, so the guard skips the include entirely.
    """
    return ("ifndef ELD_SHIM_LD\n"
            f"include $(dir $(firstword $(MAKEFILE_LIST))){eld_mk_rel}\n"
            "endif\n")


# Names `make` is actually invoked on. A `.mk` at the tree root is still an
# included fragment, not an entry point, so it does not need the include.
ENTRY_POINT_NAMES = ("Makefile", "makefile", "GNUmakefile", "Makefile.in")  # .in: hand-written ./configure templates


def needs_include(original: str, converted: str, depth: int, name: str = "Makefile") -> bool:
    """True if this makefile must pull in eld.mk itself.

    Entry-point makefiles do; so does any makefile that now refers to
    $(ELD_SHIM_LD) and might be built standalone. The rest inherit the
    variables from the top-level make, so leaving them untouched keeps the
    conversion small and avoids an include landing in an awkward place.
    """
    if depth == 0 and name in ENTRY_POINT_NAMES:
        return True
    return "$(ELD_SHIM_LD)" in converted
