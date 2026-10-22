"""Rule table mapping bfd/lld/gold/mold linker options to eld.

Actions
  KEEP         option is accepted by eld with the same spelling
  RENAME       option exists under a different spelling (target may use {value})
  DROP         no eld equivalent, dropping is harmless (tuning/diagnostic/tool-specific)
  UNSUPPORTED  no eld equivalent and dropping changes semantics -> error-level diag
Rules flagged `tentative` are best-effort; `eldconv check` confirms them against
`ld.eld --help`.  Unknown options are kept and reported (info level).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Dict, List, Optional, Sequence, Tuple

from .flags import LinkArg

ALL = frozenset({"bfd", "lld", "gold", "mold"})


class Action(str, Enum):
    KEEP = "keep"
    RENAME = "rename"
    DROP = "drop"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Rule:
    name: str                       # glob matched against LinkArg.name
    action: Action
    target: Optional[str] = None    # RENAME: new name (may reference {value})
    value: Optional[str] = None     # glob matched against LinkArg.value (None = any)
    linkers: frozenset = ALL
    note: str = ""
    tentative: bool = False
    level: Optional[str] = None     # RENAME: diagnostic level ("warn" if meaning changes)
    passthrough: bool = False       # KEEP even though eld rejects it (not meant for eld at all)


@dataclass
class Diag:
    level: str          # "info" | "warn" | "error"
    flag: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    kind: Optional[str] = field(default=None, compare=False)   # internal tag, not reported

    def as_dict(self) -> dict:
        return {"level": self.level, "flag": self.flag, "message": self.message,
                "file": self.file, "line": self.line}


def K(name, note="", value=None, linkers=ALL, tentative=False):
    return Rule(name, Action.KEEP, None, value, frozenset(linkers), note, tentative)


def R(name, target, note="", value=None, linkers=ALL, tentative=False, level=None):
    return Rule(name, Action.RENAME, target, value, frozenset(linkers), note, tentative, level)


def D(name, note="", value=None, linkers=ALL, tentative=False):
    return Rule(name, Action.DROP, None, value, frozenset(linkers), note, tentative)


def U(name, note="", value=None, linkers=ALL, tentative=False):
    return Rule(name, Action.UNSUPPORTED, None, value, frozenset(linkers), note, tentative)


LLD = {"lld"}
GOLD = {"gold"}
MOLD = {"mold"}
BFD = {"bfd"}

# Order matters: first match wins.
RULES: List[Rule] = [
    # ---- driver-level linker selection: replaced by the eld shim (-B eld-bin/)
    D("-fuse-ld", "linker selection replaced by eld shim"),
    D("--ld-path", "linker selection replaced by eld shim"),

    # ---- -z keywords
    U("-z", "RELR packed relocations not supported by eld", value="pack-relative-relocs"),
    U("-z", "RELR packed relocations not supported by eld", value="nopack-relative-relocs"),
    D("-z", "x86 CET options not supported by eld", value="ibt"),
    D("-z", "x86 CET options not supported by eld", value="shstk"),
    D("-z", "x86 CET options not supported by eld", value="cet-report*"),
    D("-z", "lld/mold-only tuning", value="rewrite-endbr"),
    D("-z", "lld-only", value="keep-text-section-prefix"),
    D("-z", "lld-only", value="dead-reloc-in-nonalloc*"),
    D("-z", "lld/mold-only", value="start-stop-visibility*"),
    D("-z", "lld/mold-only", value="lrodata-after-bss"),
    K("-z", "confirmed accepted by ld.eld 24.0", value="nognustack"),
    K("-z", value="now"), K("-z", value="lazy"), K("-z", value="relro"), K("-z", value="norelro"),
    K("-z", value="defs"), K("-z", value="muldefs"),
    U("-z", "not accepted by eld 24.0; permits undefined symbols in shared objects, so "
            "dropping it silently makes the link strict again", value="undefs"),
    K("-z", value="nocopyreloc"), K("-z", value="execstack"), K("-z", value="noexecstack"),
    K("-z", value="text"), K("-z", value="notext"),
    U("-z", "not accepted by eld 24.0; use -z notext instead (same effect)", value="textoff"),
    K("-z", value="max-page-size=*"), K("-z", value="common-page-size=*"),
    K("-z", value="combreloc"), K("-z", value="nocombreloc"),
    K("-z", "confirmed accepted by ld.eld 24.0", value="separate-code"),
    K("-z", "confirmed accepted by ld.eld 24.0 (note: not the hyphenated spelling)", value="noseparate-code"),
    D("-z", "not accepted by eld 24.0; DF_ORIGIN hint, safe to drop", value="origin"),
    K("-z", value="global", tentative=True),
    K("-z", value="initfirst", tentative=True), K("-z", value="nodelete", tentative=True),
    K("-z", value="force-bti", tentative=True), K("-z", value="pac-plt", tentative=True),
    K("-z", "unknown -z keyword; verify with probe", tentative=True),

    # ---- lld-specific
    U("--icf", "eld has no identical-code-folding; binary size may grow", linkers=LLD | GOLD | MOLD),
    D("--no-icf", linkers=LLD | GOLD | MOLD),
    D("--print-icf-sections", linkers=LLD | MOLD),
    D("--icf-iterations", linkers=GOLD),
    U("--pack-dyn-relocs", "RELR/Android packed relocs not supported by eld", linkers=LLD | MOLD),
    U("--compress-debug-sections", "eld has no output-side section compression", linkers=ALL),
    U("--compress-sections", "eld has no output-side section compression", linkers=LLD),
    D("--gdb-index", "not supported by eld", linkers=LLD | GOLD),
    D("--color-diagnostics", linkers=LLD | MOLD), D("--no-color-diagnostics", linkers=LLD | MOLD),
    D("--time-trace*", linkers=LLD), D("--warn-backrefs*", linkers=LLD),
    K("--rosegment"), D("--no-rosegment", "eld has --rosegment but not its negation",
                        linkers=LLD | GOLD | MOLD),
    D("--call-graph-profile-sort", linkers=LLD), D("--no-call-graph-profile-sort", linkers=LLD),
    D("--call-graph-ordering-file", linkers=LLD),
    D("--symbol-ordering-file", "lld-only; eld orders via linker script/plugins", linkers=LLD),
    K("--thinlto-jobs"),
    D("--thinlto-cache-dir", linkers=LLD), D("--thinlto-cache-policy", linkers=LLD),
    K("--lto-O*", "eld accepts lld's --lto-O<n>"),
    D("--lto-CGO*", linkers=LLD), K("--lto-emit-asm", linkers=LLD), K("--lto-emit-llvm", linkers=LLD),
    D("--lto-whole-program-visibility", linkers=LLD), K("--lto-partitions", linkers=LLD),
    K("--lto-sample-profile"), D("--lto-basic-block-sections", linkers=LLD),
    D("--lto-unique-basic-block-section-names", linkers=LLD), D("--lto-validate-all-vtables-have-type-infos", linkers=LLD),
    D("--why-extract", linkers=LLD), D("--chroot", linkers=LLD),
    D("--dependency-file", linkers=LLD | BFD), D("--print-archive-stats", linkers=LLD),
    D("--check-dynamic-relocations", linkers=LLD), D("--no-check-dynamic-relocations", linkers=LLD),
    D("--shuffle-sections", linkers=LLD | MOLD), D("--strip-lto-sections", linkers=LLD),
    D("--errata*", linkers=LLD), D("--android-memtag*", linkers=LLD),
    D("--randomize-section-padding", linkers=LLD), D("--pcrel-optimize", linkers=LLD),
    D("--toc-optimize", linkers=LLD), D("--no-toc-optimize", linkers=LLD),
    D("--power10-stubs*", linkers=LLD), D("--no-power10-stubs", linkers=LLD),
    D("--mllvm", "LLVM backend options; pass via --lto-options if needed", linkers=LLD, tentative=True),
    K("--plugin-opt"),
    D("--warn-ifunc-textrel", linkers=LLD), D("--no-warn-symbol-ordering", linkers=LLD),
    D("--warn-symbol-ordering", linkers=LLD), K("--no-warn-mismatch", "confirmed accepted by ld.eld 24.0"),

    # ---- gold-specific
    D("--incremental*", "gold incremental linking not supported", linkers=GOLD),
    D("--no-incremental*", linkers=GOLD),
    D("--split-stack-adjust-size", linkers=GOLD), D("--no-map-whole-files", linkers=GOLD),
    D("--map-whole-files", linkers=GOLD), D("--ld-generated-unwind-info", linkers=GOLD),
    D("--no-ld-generated-unwind-info", linkers=GOLD), D("--section-ordering-file", linkers=GOLD),
    D("--preread-archive-symbols", linkers=GOLD), D("--detect-odr-violations", linkers=GOLD),
    D("--no-detect-odr-violations", linkers=GOLD), D("--text-reorder", linkers=GOLD),
    D("--no-text-reorder", linkers=GOLD), D("--debug", linkers=GOLD),
    D("--stats", linkers=GOLD | MOLD), D("--no-keep-memory", linkers=GOLD | BFD),
    D("--keep-files-mapped", linkers=GOLD), D("--no-keep-files-mapped", linkers=GOLD),
    D("--mmap-output-file", linkers=GOLD), D("--no-mmap-output-file", linkers=GOLD),
    D("--posix-fallocate", linkers=GOLD), D("--no-posix-fallocate", linkers=GOLD),
    D("--weak-unresolved-symbols", linkers=GOLD), D("--no-weak-unresolved-symbols", linkers=GOLD),

    # ---- mold-specific
    D("--fork", linkers=MOLD), D("--no-fork", linkers=MOLD),
    D("--perf", linkers=MOLD), D("--quick-exit", linkers=MOLD), D("--no-quick-exit", linkers=MOLD),
    R("--repro", "--reproduce=eld-repro.tar", "eld: --reproduce=<tar>", linkers=MOLD, tentative=True),
    D("--run", "mold -run wrapper removed", linkers=MOLD),
    D("--print-dependencies", linkers=MOLD), D("--relocatable-merge-sections", linkers=MOLD),
    D("--separate-debug-file", linkers=MOLD), D("--no-separate-debug-file", linkers=MOLD),
    D("--package-metadata", linkers=MOLD | BFD | LLD), D("--spare-dynamic-tags", linkers=MOLD),
    D("--spare-program-headers", linkers=MOLD), D("--apply-dynamic-relocs", linkers=MOLD | LLD),
    D("--no-apply-dynamic-relocs", linkers=MOLD | LLD), D("--dependency-file", linkers=MOLD),
    D("--detach", linkers=MOLD), D("--no-detach", linkers=MOLD),
    D("--z-*", linkers=MOLD), K("--warn-once", "confirmed accepted by ld.eld 24.0", linkers=MOLD | BFD),


    # ---- bfd-specific
    K("--print-memory-usage"),
    K("--no-warn-rwx-segments"), K("--warn-rwx-segments"),
    D("--warn-execstack", linkers=BFD), D("--no-warn-execstack", linkers=BFD),
    D("--ctf-*", linkers=BFD), D("--gc-keep-exported", linkers=BFD),
    D("--enable-non-contiguous-regions", linkers=BFD), D("--enable-non-contiguous-regions-warnings", linkers=BFD),
    D("--reduce-memory-overheads", linkers=BFD), D("--warn-section-align", linkers=BFD),
    K("--orphan-handling", "diagnostics-only; verified by probe", tentative=True), D("--no-define-common", linkers=BFD),
    K("--default-script"),
    # eld 24.0 rejects `-dT`/`--dT` outright ("Unrecognized option") in every form
    # tested (bare, `=value`, space-separated) even though it lists --default-script;
    # rename to the synonym it does accept rather than silently dropping the script.
    R("-dT", "--default-script={value}", "eld does not accept -dT; --default-script is its synonym"),
    R("--dT", "--default-script={value}", "eld does not accept --dT; --default-script is its synonym"),
    D("--no-print-map-discarded", linkers=BFD), D("--print-map-discarded", linkers=BFD),
    D("--print-map-locals", linkers=BFD), D("--no-print-map-locals", linkers=BFD),
    D("--warn-textrel", linkers=BFD), D("--warn-alternate-em", linkers=BFD),
    K("--disable-linker-version"), K("--enable-linker-version"),
    K("-plugin", "eld accepts it; the shim strips GCC's collect2 plugin anyway"),
    K("-plugin-opt", "eld accepts it; the shim strips GCC's collect2 plugin anyway"),

    # ---- verified against ld.eld 24.0: accepted-and-ignored, or absent
    # --pic-veneer: eld before late Sep 2026 ignored it silently; the current eld implements
    # it for ARM and AArch64. The surface decides (an eld that lacks it gets the option
    # dropped as effect-lost, see mapping.drop_severity).
    K("--pic-veneer", "implemented by eld's ARM and AArch64 backends (newer eld builds only)"),
    K("--fix-cortex-a53-843419", "implemented by eld's AArch64 backend (ARM/AArch64 links only)"),
    U("--fix-cortex-a53-835769", "erratum workaround not implemented by eld 24.0; affected "
                                 "Cortex-A53 parts can miscompute multiply-accumulates"),

    # ---- PE/COFF (Windows) only: they only ever appear in a project's Windows
    # branch, which eld (an ELF linker) is not used for, so they are left alone.
    *[Rule(n, Action.KEEP, note="PE/COFF (Windows) only; left untouched -- eld does not link "
                                 "Windows targets, so this belongs to a branch eld is not used for",
           passthrough=True)
      for n in ("--force-exe-suffix", "--out-implib", "--output-def", "--export-all-symbols", "--exclude-all-symbols",
                "--enable-auto-import", "--disable-auto-import", "--enable-runtime-pseudo-reloc",
                "--disable-runtime-pseudo-reloc", "--enable-stdcall-fixup", "--disable-stdcall-fixup",
                "--add-stdcall-alias", "--kill-at", "--subsystem", "--major-image-version",
                "--minor-image-version", "--major-os-version", "--minor-os-version",
                "--major-subsystem-version", "--minor-subsystem-version", "--file-alignment",
                "--section-alignment", "--large-address-aware", "--disable-large-address-aware",
                "--dll", "--dynamicbase", "--nxcompat", "--high-entropy-va", "--no-seh", "--tsaware",
                "--enable-auto-image-base", "--disable-auto-image-base", "--exclude-symbols",
                "--exclude-modules-for-implib", "--compat-implib", "--dll-search-prefix",
                "--enable-long-section-names", "--disable-long-section-names", "--stack", "--heap",
                "--base_file", "--default-imported-symver", "--warn-duplicate-exports",
                "--enable-extra-pe-debug", "--disable-reloc-section", "--enable-reloc-section",
                "--insert-timestamp", "--no-insert-timestamp", "--build-id-pe")],

    # ---- verified against ld.eld 24.0: different spelling or a safe default
    R("--require-defined", "--undefined", "eld has no --require-defined; --undefined still pulls "
      "the symbol in but no longer fails the link if nothing defines it", level="warn"),
    R("--no-pic-executable", "--no-pie", "gold/mold spelling of --no-pie"),
    R("-I", "--dynamic-linker", "single-letter alias eld does not have"),
    D("-Bno-symbolic", "eld default (no symbolic binding)"), D("--Bno-symbolic", "eld default"),
    D("--no-allow-multiple-definition", "eld default"),
    D("--discard-none", "eld has no --discard-none; local symbols other than .L temporaries are kept"),
    D("--no-warnings", "diagnostics only"), D("-w", "diagnostics only"),
    D("--oformat", "eld picks the ELF flavour from its inputs", value="elf*"),
    U("--oformat", "eld only writes ELF (use objcopy -O for raw/ihex/srec output)"),

    # ---- generic GNU surface accepted by eld
    K("--gc-sections"), K("--no-gc-sections"), K("--print-gc-sections"), D("--no-print-gc-sections", "eld has --print-gc-sections but not its negation"),
    K("-Map"), R("--Map", "-Map"), K("-T"), K("--script"), K("-e"), K("--entry"), K("-o"),
    K("-L"), K("-l"), K("-m"), K("-u"), K("--undefined"), K("--wrap"), K("--defsym"),
    K("--version-script"), K("--dynamic-linker"), K("-rpath"), K("--rpath"),
    K("-rpath-link"), K("--rpath-link"), K("--as-needed"), K("--no-as-needed"),
    K("--whole-archive"), K("--no-whole-archive"), K("--start-group"), K("--end-group"),
    K("-("), K("-)"), K("--strip-all"), K("-s"), K("--strip-debug"), K("-S"),
    K("--emit-relocs"), R("-q", "--emit-relocs", "eld only has the long spelling"),
    K("--allow-multiple-definition"), K("--no-undefined"),
    K("--unresolved-symbols"), K("--warn-common"), D("--no-warn-common", "eld has --warn-common but not its negation"), K("--fatal-warnings"),
    K("--no-fatal-warnings"), K("-Bsymbolic"), K("--Bsymbolic"), K("-Bsymbolic-functions"),
    K("--Bsymbolic-functions"), K("-Bstatic"), K("-Bdynamic"), K("--export-dynamic"), K("-E"),
    K("--no-export-dynamic"), K("--exclude-libs"), K("--hash-style"), K("--build-id"),
    K("--sort-section"), K("--relax"), K("--no-relax"), K("--verbose"), K("--trace"), K("-t"),
    K("--demangle"), K("--no-demangle"), K("--check-sections"), K("--no-check-sections"),
    K("-soname"), K("--soname"), K("-h"), K("--image-base"), K("--section-start"),
    K("-Ttext"), K("-Tdata"), K("-Tbss"), K("--threads"), K("--no-threads"), K("--thread-count"),
    K("--reproduce"), K("--error-limit"), K("--discard-all"), K("-x"), K("--discard-locals"),
    K("-X"), K("--sysroot"), K("--plugin"), K("--plugin-config"),
    D("-O", "no effect in eld (current eld accepts and ignores -O<n>, older builds warn); it only "
            "tunes hash-table sizing in bfd, so dropping it changes no behaviour (common in "
            "distro LDFLAGS as -Wl,-O1)"),
    K("-shared"),
    K("--shared"), K("-static"), K("--static"), K("-pie"), K("--pie"), K("--no-pie"),
    K("--dynamic-list"), K("--print-map"), K("-M"), K("--cref"), K("--trace-symbol"), K("-y"),
    K("--gc-cref"), K("--save-temps"), R("--lto-options", "--flto-options"), K("--flto-options"), K("--trampoline-map"),
    K("--symdef-file"), K("--copy-farcalls-from-file"), K("--eh-frame-hdr"), D("--no-eh-frame-hdr", "eld has --eh-frame-hdr but not its negation"),
    K("--nmagic"), K("-n"), K("--omagic"), K("-N"), K("-r"), R("--relocatable", "-r", "eld spells this -r"), R("-relocatable", "-r", "eld spells this -r"),
    K("--start-lib", tentative=True), K("--end-lib", tentative=True),
    K("--push-state", tentative=True), K("--pop-state", tentative=True),
    D("--retain-symbols-file", "not accepted by eld 24.0"), K("--just-symbols", tentative=True), K("-R", tentative=True),
    K("--allow-shlib-undefined", tentative=True), K("--no-allow-shlib-undefined", tentative=True),
    K("--export-dynamic-symbol", tentative=True),     D("--warn-unresolved-symbols", "use --unresolved-symbols instead"),
    D("--error-unresolved-symbols", "use --unresolved-symbols instead"),
    K("--no-undefined-version", "accepted by current eld (older builds reject it)"),
    K("--undefined-version", "accepted by current eld (older builds reject it)"),
    K("--merge-exidx-entries", "ARM; accepted by current eld", linkers=("lld",)),
    K("--no-merge-exidx-entries", "ARM; accepted by current eld", linkers=("lld",)),
    K("--copy-dt-needed-entries", tentative=True), K("--no-copy-dt-needed-entries", tentative=True),
    D("--unique", "not accepted by eld 24.0"), K("--disable-new-dtags", tentative=True),
    K("--enable-new-dtags", tentative=True), K("--noinhibit-exec", tentative=True),
    K("--unique-output-sections", tentative=True),
]


# find_rule is on the hot path (every flag of every build file), and a linear fnmatch over the
# whole table cost ~60 us per flag. The rules that can match an option name are the same every
# time, so they are computed once per (name, linker), in table order (first match wins).
_CANDIDATES: Dict[Tuple[str, str], Tuple[Rule, ...]] = {}
_CANDIDATES_KEY: List[int] = [0, 0]


def _candidates(name: str, linker: str) -> Tuple[Rule, ...]:
    key = [id(RULES), len(RULES)]
    if _CANDIDATES_KEY != key:            # the table was replaced or extended (tests do this)
        _CANDIDATES.clear()
        _CANDIDATES_KEY[:] = key
    hit = _CANDIDATES.get((name, linker))
    if hit is None:
        hit = tuple(r for r in RULES if linker in r.linkers and fnmatchcase(name, r.name))
        if len(_CANDIDATES) < 50000:
            _CANDIDATES[(name, linker)] = hit
    return hit


def find_rule(arg: LinkArg, linker: str) -> Optional[Rule]:
    for r in _candidates(arg.name, linker):
        if r.value is not None and not fnmatchcase(arg.value or "", r.value):
            continue
        return r
    return None


def _spelling(arg: LinkArg) -> str:
    return " ".join(arg.parts())


# Options eld rejects whose loss changes nothing a user would ship: diagnostics,
# progress output, linker-internal performance knobs, and spellings of eld's own
# defaults. Anything else eld rejects is reported as an error.
_HARMLESS_OPT_RE = re.compile(
    r"^--?(?:no-)?warn(?:ings|-)|^-w$|^--(?:no-)?(?:print|verbose)-|^--(?:no-)?stats$"
    r"|^--(?:no-)?colou?r|^--vs-diagnostics$|^--rsp-quoting$|^--(?:no-)?threads?\b|^--thread-count"
    r"|^--time-trace|^--why-|^--error-limit$|^--error-handling-script$|^--(?:no-)?keep-memory$"
    r"|^--reduce-memory-overheads$|^--(?:no-)?(?:mmap-output-file|map-whole-files|keep-files-mapped"
    r"|posix-fallocate|fork|quick-exit|detach|gdb-index|debug-names)$|^--perf$"
    r"|^--hash-bucket-empty-fraction$|^--dependency-file$|^--target-help$|^--print-sysroot$"
    r"|^--no-(?:cref|nmagic|omagic|build-id|fix-[\w-]+|strip-discarded)$|^--strip-discarded$"
    r"|^--(?:no-)?error-(?:execstack|rwx-segments)$|^--build-id-[\w-]*treehash$|^--package-metadata$"
    r"|^--no-enum-size-warning$|^--no-wchar-size-warning$|^--no-print-icf-sections$")
_HARMLESS_Z = frozenset({
    "sectionheader", "mark-plt", "nomark-plt", "report-relative-reloc", "isa-level-report",
    "lam-report", "lam-u48-report", "lam-u57-report", "bti-report", "gcs-report", "cet-report",
    "nopac-plt", "nokeep-text-section-prefix", "nolrodata-after-bss", "norewrite-endbr",
    "copyreloc", "buildd", "text-unlikely-segment", "notext-unlikely-segment", "nobtcfi",
    "wxneeded", "call-nop", "ibt", "shstk", "ibtplt", "force-ibt", "x86-64-baseline",
    "x86-64-v2", "x86-64-v3", "x86-64-v4", "nounique", "nounique-symbol", "nostart-stop-gc",
    "execstack-if-needed", "nodynamic-undefined-weak",
})


def drop_severity(arg: LinkArg) -> str:
    """How bad it is to lose an option eld does not accept: "warn" or "error"."""
    if arg.name == "-z":
        return "warn" if (arg.value or "").split("=", 1)[0] in _HARMLESS_Z else "error"
    return "warn" if _HARMLESS_OPT_RE.search(arg.name) else "error"


def _eld_label() -> str:
    from .surface import active
    s = active()
    m = re.search(r"eld\s+([\w.\-]+)", s.eld_version if s else "")
    return f"eld {m.group(1)}" if m else "this eld"


def _surface_verdict(arg: LinkArg, target: Optional[str] = None) -> Optional[bool]:
    from .surface import active
    s = active()
    return s.accepts(arg.name, arg.value, target) if s else None


def _unaccepted(arg: LinkArg, spelled: str, target: Optional[str] = None) -> Tuple[List[LinkArg], List[Diag]]:
    from .surface import active
    s = active()
    others = (s.accepting(arg.name, arg.value) or []) if s and target else []
    if others:
        # The backends that take it are the ones it means something to (ARM errata,
        # veneers, small data, relaxation): for this target there is nothing to lose.
        return [], [Diag("warn", spelled,
                         f"dropped: {_eld_label()} accepts it only for {', '.join(others)} links; "
                         f"it would ignore it in {target} links (\"Unrecognized option\")")]
    level = drop_severity(arg)
    tail = "" if level == "warn" else "; its effect is lost"
    return [], [Diag(level, spelled,
                     f"dropped: not accepted by {_eld_label()}, which would ignore it"
                     f" (\"Unrecognized option\"){tail}")]


def convert_arg(arg: LinkArg, linker: str, target: Optional[str] = None) -> Tuple[List[LinkArg], List[Diag]]:
    """Convert one linker option. Returns (replacement args, diagnostics).

    The rule table says what an option *means* for eld; the active eld surface
    (surface.py) says whether this eld accepts it at all -- for `target` when
    that is known, else for any target. An option the table would keep -- or
    does not know -- but eld rejects is dropped and reported, because eld would
    otherwise ignore it with only a warning.
    """
    if arg.positional:
        return [arg], []
    rule = find_rule(arg, linker)
    spelled = _spelling(arg)
    if rule is None:
        verdict = _surface_verdict(arg, target)
        if verdict is False:
            return _unaccepted(arg, spelled, target)
        if verdict is True:
            return [arg], []
        return [arg], [Diag("info", spelled, "unknown to converter; kept as-is, verify with `eldconv check`")]
    tent = " (tentative: confirmed at check time)" if rule.tentative else ""
    if rule.action is Action.KEEP:
        if rule.passthrough:
            return [arg], [Diag("info", spelled, rule.note)]
        verdict = _surface_verdict(arg, target)
        if verdict is False:
            return _unaccepted(arg, spelled, target)
        if rule.tentative and verdict is None:
            return [arg], [Diag("info", spelled, f"kept{tent}")]
        return [arg], []
    if rule.action is Action.RENAME:
        tgt = rule.target or arg.name
        if "{value}" in tgt:
            new = LinkArg(tgt.format(value=arg.value or ""))
        else:
            new = LinkArg(tgt, arg.value, arg.sep, arg.positional)
        level = rule.level or "info"
        return [new], [Diag(level, spelled, f"renamed to `{_spelling(new)}`{tent}"
                                            f"{'; ' + rule.note if rule.note else ''}")]
    if rule.action is Action.DROP:
        return [], [Diag("warn", spelled, f"dropped: {rule.note or 'no eld equivalent'}{tent}")]
    # UNSUPPORTED
    return [], [Diag("error", spelled, f"dropped, semantics lost: {rule.note}{tent}")]


def link_target(args: Sequence[LinkArg]) -> Optional[str]:
    """The target these options are for: their own `-m EMULATION` (the last one wins, as
    in ld), else the conversion's `--target`, else None (any target)."""
    from .surface import active_target, emulation_target
    for a in reversed(args):
        if a.name == "-m" and not a.positional and a.value:
            t = emulation_target(a.value)
            if t:
                return t
    return active_target()


def convert_args(args: Sequence[LinkArg], linker: str) -> Tuple[List[LinkArg], List[Diag]]:
    from .flags import NO_VALUE_ON_NEXT, VALUE_FLAGS
    out: List[LinkArg] = []
    diags: List[Diag] = []
    target = link_target(args)
    for a in args:
        r, d = convert_arg(a, linker, target)
        out.extend(r)
        diags.extend(d)
    # An option that requires a value but has none left is worse than useless:
    # the linker will consume whatever follows (often an input file) as its value.
    # Only a value *lost in conversion* is reported: `-Wl,-T $(lds)` is written
    # that way in the source (the script is the driver's next word) and unchanged.
    def dangling(seq):
        res = []
        for i, a in enumerate(seq):
            if a.positional or a.value is not None or a.name not in VALUE_FLAGS:
                continue
            if a.name in NO_VALUE_ON_NEXT:
                continue  # e.g. `--build-id` is legal on its own
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            if nxt is None or not nxt.positional:
                res.append(a)
        return res
    seq0 = list(args)
    originally = {a.name for a in dangling(seq0) if a is seq0[-1]}  # value is the driver's next word
    for a in dangling(out):
        if a.name in originally:
            continue
        diags.append(Diag("error", a.name,
                          "requires a value but none survived conversion; "
                          "check this option by hand"))
    return out, diags


def rule_names() -> List[str]:
    return [r.name for r in RULES]
