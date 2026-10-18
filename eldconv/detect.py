"""Detect build system, toolchain family and current linker of a source tree."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import LINKERS
from .log import NULL_LOG, Log

CMAKE_FILES = ("CMakeLists.txt",)
CMAKE_GLOBS = ("*.cmake", "CMakeCache.txt", "CMakePresets.json", "CMakeUserPresets.json")
MAKE_FILES = ("Makefile", "makefile", "GNUmakefile", "Kbuild", "Kconfig.include")
MAKE_GLOBS = ("*.mk", "*.make", "Makefile.*", "Kbuild.*", "Kbuild")
GN_FILES = ("BUILD.gn", ".gn")
GN_GLOBS = ("*.gn", "*.gni")
NINJA_GLOBS = ("build.ninja", "*.ninja")
MESON_FILES = ("meson.build", "meson_options.txt", "meson.options")
# Meson machine (native/cross) files; only counted when they look like one.
MESON_MACHINE_GLOBS = ("*.ini",)
AUTOCONF_FILES = ("configure.ac", "configure.in")
AUTOMAKE_GLOBS = ("Makefile.am", "*.am")
VCS_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".cache"}
# Directory names that *may* hold build output; confirmed by looks_like_build_output().
SKIP_DIRS = VCS_DIRS | {"build", "_build", "out", "cmake-build-debug", "cmake-build-release"}

_LINKER_WORDS = "|".join(LINKERS)
PATTERNS = [
    ("fuse", re.compile(r"-fuse-ld=(" + _LINKER_WORDS + r")\b")),
    # clang also takes a path: -fuse-ld=/usr/bin/ld.lld
    ("fuse_path", re.compile(r"-fuse-ld=[^\s\"']*/(?:ld\.)?(" + _LINKER_WORDS + r")\b")),
    # mold's recipe for GCC < 12: -B/usr/libexec/mold
    ("b_dir", re.compile(r"(?<![\w-])-B\s*[^\s\"']*?(?:libexec/)?(mold|lld|gold)/?(?=[\s\"',;)\]]|$)")),
    # CMakePresets.json: "CMAKE_LINKER_TYPE": "LLD" or {"type": ..., "value": "LLD"}
    ("preset_type", re.compile(r'"CMAKE_LINKER_TYPE"\s*:\s*(?:\{[^}]*?"value"\s*:\s*)?"(LLD|GOLD|BFD|MOLD)"', re.I)),
    # a recipe that runs a linker itself: `\tld.gold -r -o x.o ...`
    ("recipe_ld", re.compile(r"^\t[@+\-]*\s*(?:\$\(Q\)\s*)?(?:[^\s]*/)?(?:\$[({]\w+[)}])?"
                             r"(?:ld\.(bfd|gold|lld)|(mold))(?:-[\d.]+)?(?=\s|$)", re.M)),
    ("ldpath", re.compile(r"--ld-path=\S*?(?:ld\.)?(" + _LINKER_WORDS + r")\b")),
    ("cmake_type", re.compile(r"CMAKE_LINKER_TYPE[\s=:]+\"?(LLD|GOLD|BFD|MOLD)\b", re.I)),
    ("cmake_linker", re.compile(r"CMAKE_LINKER[\s=:]+[^\s)\"]*?(?:ld\.)?(" + _LINKER_WORDS + r")\b")),
    ("linker_type_prop", re.compile(r"\bLINKER_TYPE\s+\"?(LLD|GOLD|BFD|MOLD)\b", re.I)),
    ("make_ld", re.compile(r"^\s*(?:export\s+|override\s+)?LD\s*[:?+]?=\s*[^\n#]*?(?:ld\.)?(" + _LINKER_WORDS + r")\b", re.M)),
    # plain `LD = ld` / `LD = $(CROSS_COMPILE)ld` selects GNU bfd
    ("make_ld_bfd", re.compile(r"^\s*(?:export\s+|override\s+)?LD\s*[:?+]?=\s*[^\n#]*?(?<![\w.])(ld)(?![\w.-])\s*(?:#.*)?$", re.M)),
    ("gn_use_lld", re.compile(r"^\s*use_lld\s*=\s*true", re.M)),
    ("gn_use_mold", re.compile(r"^\s*use_mold\s*=\s*true", re.M)),
    ("gn_use_gold", re.compile(r"^\s*use_gold\s*=\s*true", re.M)),
    ("meson_ld", re.compile(r"^\s*(?:c|cpp|objc|objcpp|fortran)_ld\s*=\s*\[?\s*'[^'\n]*?"
                            r"(?<![\w])(?:ld\.)?(" + _LINKER_WORDS + r")(?![\w])[^'\n]*'", re.M)),
    ("mold_run", re.compile(r"\bmold\s+-run\b")),
    ("mold_bin", re.compile(r"(?<![\w.-])mold(?![\w.-])")),
]
# A compiler name only counts when it looks like a *command*, not a filename or
# a path: `empty.cc` and `config/c++/c++.gni` are not evidence of GCC.
_NOT_FILENAME = r"(?<![\w.\-/])"
_NOT_PATH = r"(?![\w.\-]|/)"
COMPILER_PATTERNS = [
    # weight 2: naming clang or gcc explicitly is a deliberate choice
    ("llvm", 2, re.compile(_NOT_FILENAME + r"clang(\+\+)?" + _NOT_PATH)),
    ("gnu", 2, re.compile(_NOT_FILENAME + r"(?:gcc|g\+\+)" + _NOT_PATH)),
    ("gnu", 2, re.compile(r"CROSS_COMPILE|-gcc\b|-g\+\+\b")),
    # weight 1: the generic driver names, and only as an assignment value
    ("gnu", 1, re.compile(r"(?:^|[=\s\"'])(?:CC|CXX)\s*[:?+]?=\s*[\"']?(?:cc|c\+\+)" + _NOT_PATH, re.M)),
]


@dataclass
class BuildInfo:
    root: Path
    system: str = "unknown"      # cmake | make | gn | ninja | meson | autotools | unknown
    toolchain: str = "unknown"   # gnu | llvm | unknown
    linker: str = "unknown"      # bfd | lld | gold | mold | unknown
    linker_assumed: bool = False
    ambiguous: bool = False
    conditional_linkers: List[str] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"root": str(self.root), "system": self.system, "toolchain": self.toolchain,
                "linker": self.linker, "linker_assumed": self.linker_assumed,
                "ambiguous": self.ambiguous, "conditional_linkers": self.conditional_linkers,
                "files": [str(f.relative_to(self.root)) for f in self.files],
                "evidence": self.evidence}


def looks_like_build_output(d: Path) -> bool:
    """True if `d` holds build output rather than source.

    A directory merely *named* build or out is not enough: Chromium keeps its
    whole build configuration in build/, and the kernel keeps source in
    tools/build/. Require evidence of generated content.
    """
    markers = ("CMakeCache.txt", "CMakeFiles", "build.ninja", "compile_commands.json",
               ".ninja_deps", ".ninja_log", "Makefile.cmake")

    def marked(x: Path) -> bool:
        if any((x / m).exists() for m in markers):
            return True
        try:
            return any(p.suffix in (".o", ".obj", ".a") for p in x.iterdir())
        except OSError:
            return False

    if marked(d):
        return True
    # Output usually sits one level down (out/Release, out/Default, build/x64).
    try:
        return any(c.is_dir() and marked(c) for c in d.iterdir())
    except OSError:
        return False


_BUILD_NAMES = frozenset(CMAKE_FILES + MAKE_FILES + GN_FILES + MESON_FILES + AUTOCONF_FILES)
# Every glob is a plain file-name pattern, so one compiled regex on the name replaces a
# Path.match() per glob per file (the latter dominated conversion time on large trees).
_BUILD_GLOB_RE = re.compile("|".join(fnmatch.translate(g) for g in
                                     CMAKE_GLOBS + MAKE_GLOBS + GN_GLOBS + NINJA_GLOBS + AUTOMAKE_GLOBS))
_MESON_MACHINE_RE = re.compile("|".join(fnmatch.translate(g) for g in MESON_MACHINE_GLOBS))


def iter_build_files(root: Path, skip_build_dirs: bool = True) -> List[Path]:
    """Build files under root, sorted.

    skip_build_dirs=True (the default) ignores VCS and *genuine* build output
    directories. Pass False to find build files inside them, e.g. to report ones
    that were copied but never converted. Symlinked directories are not followed.
    """
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_build_dirs:
            keep = []
            for d in dirnames:
                if d in VCS_DIRS:
                    continue
                if d in SKIP_DIRS and looks_like_build_output(Path(dirpath, d)):
                    continue
                keep.append(d)
            dirnames[:] = keep
        for name in filenames:
            if name in _BUILD_NAMES or _BUILD_GLOB_RE.match(name):
                pass
            elif _MESON_MACHINE_RE.match(name):
                if not _is_meson_machine_file(Path(dirpath, name)):
                    continue
            else:
                continue
            full = os.path.join(dirpath, name)
            if os.path.isfile(full):          # follows a symlink; drops broken ones
                found.append(Path(full))
    found.sort()
    return found


MAKE_IF_RE = re.compile(r"^\s*(ifdef|ifndef|ifeq|ifneq|else|endif)\b")
# kbuild idiom: `ldflags-$(CONFIG_LD_IS_LLD) += ...` is only used when that
# config symbol is set, so it is as conditional as an explicit ifdef.
MAKE_CONFIG_SUFFIX_RE = re.compile(r"^\s*[\w.-]+-\$[({][A-Za-z_][\w]*[)}]\s*[:?+]?=")
# `$(if $(LLVM),-fuse-ld=lld)` and `$(call cc-option,...)` only take effect when
# the condition holds or the probe succeeds, so they are conditional too.
# Target-specific variable: `asan: LDFLAGS += -fuse-ld=gold` only applies to that target.
MAKE_TARGET_VAR_RE = re.compile(r"^[^\t#=:\s][^#=:]*:(?!=)\s*(?:override\s+|export\s+)?[A-Za-z_][\w.-]*\s*[:+?]?=")
MAKE_COND_FUNC_RE = re.compile(r"\$[({](?:if|or|and|filter|filter-out|call\s*[\w-]*option)\b")
CMAKE_IF_RE = re.compile(r"^\s*(if|elseif|else|endif)\s*\(", re.I)
MESON_IF_RE = re.compile(r"^\s*(if|elif|else|endif)\b")


def conditional_lines(text: str, kind: str) -> set:
    """1-based line numbers that sit inside an if/else block.

    A linker chosen inside a conditional (the kernel's `LD = ld.lld` under
    `ifneq ($(LLVM),)`) is not necessarily the one the build uses by default.
    """
    rx = {"make": MAKE_IF_RE, "automake": MAKE_IF_RE, "meson": MESON_IF_RE}.get(kind, CMAKE_IF_RE)
    inside: set = set()
    depth = 0
    for i, line in enumerate(text.split("\n"), 1):
        m = rx.match(line)
        kw = m.group(1).lower() if m else None
        if kw in ("endif",):
            depth = max(0, depth - 1)
            continue
        if kw in ("else", "elseif", "elif"):
            continue
        if kw is not None:
            depth += 1
            continue
        if depth or (rx is MAKE_IF_RE and (MAKE_CONFIG_SUFFIX_RE.match(line)
                                           or MAKE_TARGET_VAR_RE.match(line)
                                           or MAKE_COND_FUNC_RE.search(line))):
            inside.add(i)
    return inside


def _is_meson_machine_file(p: Path) -> bool:
    try:
        if p.stat().st_size > (1 << 20):
            return False
        from .converters.meson import is_machine_file
        return is_machine_file(p.read_text(errors="replace"))
    except OSError:
        return False


def classify_file(p: Path) -> str:
    if p.name in MESON_FILES or p.suffix == ".ini":
        return "meson"
    if p.name in AUTOCONF_FILES:
        return "autoconf"
    if p.suffix == ".am" or (p.name == "Makefile.in" and (p.parent / "Makefile.am").exists()):
        return "automake"
    if p.name in GN_FILES or any(p.match(g) for g in GN_GLOBS):
        return "gn"
    if any(p.match(g) for g in NINJA_GLOBS):
        return "ninja"
    if p.name in CMAKE_FILES or any(p.match(g) for g in CMAKE_GLOBS):
        return "cmake"
    return "make"


SYSTEMS = ("cmake", "make", "gn", "ninja", "meson", "autotools")


def detect(root: Path, source_linker: Optional[str] = None, log: Log = NULL_LOG,
           system: Optional[str] = None) -> BuildInfo:
    root = Path(root).resolve()
    log.step(f"detecting build system under {root}")
    info = BuildInfo(root=root)
    info.files = iter_build_files(root)
    log.info(f"found {len(info.files)} build file(s)")
    kinds = {classify_file(f) for f in info.files}
    # GN generates Ninja, so a tree with both is a GN tree.
    if "gn" in kinds:
        info.system = "gn"
    elif "cmake" in kinds and (root / "CMakeLists.txt").exists():
        info.system = "cmake"
    elif (root / "meson.build").exists():
        info.system = "meson"
    elif any((root / n).exists() for n in AUTOCONF_FILES) or (root / "configure").is_file() \
            and (root / "Makefile.in").exists():
        info.system = "autotools"
    elif "ninja" in kinds:
        info.system = "ninja"
    elif "make" in kinds:
        info.system = "make"
    elif "cmake" in kinds:
        info.system = "cmake"
    if system:
        if system not in SYSTEMS:
            raise ValueError(f"unknown build system {system!r} (choose from {', '.join(SYSTEMS)})")
        if system != info.system:
            info.evidence.append(f"build system: forced to {system} by user (detected {info.system})")
        info.system = system
    log.info(f"build system: {info.system}")

    votes: dict = {}
    cond_votes: dict = {}
    tc_votes: dict = {}
    for f in info.files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root)
        log.detail(f"scanning {rel}")
        kind_of_file = classify_file(f)
        cond = (conditional_lines(text, kind_of_file)
                if kind_of_file in ("make", "cmake", "meson", "automake") else set())
        for kind, rx in PATTERNS:
            for m in rx.finditer(text):
                if kind.startswith("gn_use_"):
                    lk = kind[len("gn_use_"):]
                elif kind.startswith("mold"):
                    lk = "mold"
                elif kind == "make_ld_bfd":
                    lk = "bfd"
                elif kind == "recipe_ld":
                    lk = (m.group(1) or m.group(2)).lower()
                else:
                    lk = m.group(1).lower()
                if kind == "mold_bin" and votes:  # weak evidence
                    continue
                line = text.count("\n", 0, m.start()) + 1
                weight = 1 if kind != "mold_bin" else 0.5
                # The top-level build file is where a project picks its
                # toolchain; deeper files usually tweak one component.
                if len(rel.parts) == 1:
                    weight *= 100
                # A selection inside a recipe (`asan-%:\n\tLDFLAGS=-fuse-ld=gold $(MAKE) ...`)
                # applies to that target only, not to the build's default.
                in_recipe = (kind_of_file in ("make", "automake") and kind != "recipe_ld"
                             and text.rfind("\n", 0, m.start()) + 1 < len(text)
                             and text[text.rfind("\n", 0, m.start()) + 1] == "\t")
                if line in cond or in_recipe:
                    # e.g. the kernel's `LD = ld.lld` under `ifneq ($(LLVM),)`:
                    # a branch that is off by default is not the current linker.
                    cond_votes[lk] = cond_votes.get(lk, 0) + weight
                    info.evidence.append(f"{rel}:{line}: {kind} -> {lk} (conditional)")
                    log.detail(f"{rel}:{line}: {kind} -> {lk} (conditional, weight {weight})")
                else:
                    votes[lk] = votes.get(lk, 0) + weight
                    info.evidence.append(f"{rel}:{line}: {kind} -> {lk}")
                    log.info(f"{rel}:{line}: {kind} -> {lk} (weight {weight})")
        for tc, weight, rx in COMPILER_PATTERNS:
            if rx.search(text):
                tc_votes[tc] = tc_votes.get(tc, 0) + weight

    if tc_votes:
        info.toolchain = max(tc_votes, key=tc_votes.get)
    elif info.system == "unknown":
        # Nothing was parsed, so there is nothing to conclude. Saying "gnu/bfd"
        # here is a confident wrong answer for, say, a clang/lld GN tree.
        info.toolchain = "unknown"
        info.evidence.append("toolchain: build system not recognised, nothing scanned")
    else:
        info.toolchain = "gnu"
        info.evidence.append("toolchain: no compiler mentioned, assuming gnu")

    info.conditional_linkers = sorted(cond_votes)
    if source_linker:
        info.linker = source_linker
        info.evidence.append(f"linker: forced to {source_linker} by user")
    elif votes:
        info.linker = max(votes, key=votes.get)
        rival = [k for k in votes if k != info.linker]
        if rival:
            info.ambiguous = True
            info.evidence.append(
                f"linker: {info.linker} has the most unconditional evidence, but "
                f"{', '.join(sorted(rival))} also appear(s); confirm with --source-linker")
    elif cond_votes:
        # Every selection we found sits in a conditional branch, so the build's
        # default is whatever the toolchain uses when none of them is taken.
        info.linker = "bfd"
        info.linker_assumed = True
        info.ambiguous = True
        info.evidence.append(
            f"linker: only conditional selections found ({', '.join(sorted(cond_votes))}); "
            f"assuming the default bfd. Pass --source-linker to convert a branch instead.")
    elif info.system == "unknown":
        info.linker = "unknown"
        info.linker_assumed = True
        info.ambiguous = True
        info.evidence.append(
            "linker: build system not recognised, so no build file was scanned; "
            "pass --source-linker if you know which linker this tree uses")
    else:
        # default system linker for both gcc and (stock) clang on ELF hosts
        info.linker = "bfd"
        info.linker_assumed = True
        info.evidence.append("linker: no explicit selection found, assuming default bfd")
    log.info(f"toolchain: {info.toolchain}")
    log.info(f"source linker: {info.linker}"
             + (" (assumed)" if info.linker_assumed else "")
             + (" [ambiguous]" if info.ambiguous else ""))
    if info.conditional_linkers:
        log.info("linkers selected only inside conditionals: "
                 + ", ".join(info.conditional_linkers))
    return info
