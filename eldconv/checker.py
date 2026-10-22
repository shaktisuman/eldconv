"""Build the converted project and verify that eld actually performed the links."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
import tempfile
import time
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .elf import (ELF, SHF_ALLOC, SHN_UNDEF, SHT_DYNSYM, SHT_REL, SHT_RELA, SHT_RELR, SHT_SYMTAB,
                  STB_GLOBAL, STB_WEAK, STT_OBJECT, STT_TLS, open_elf)
from .elfcheck import validate
from .flags import parse_raw
from .log import NULL_LOG, Log
from .generator import ARGS_GN_FILE, MK_FILE, SHIM_DIR, TOOLCHAIN_FILE, load_report

CHECK_REPORT = "eldconv-check.json"
LOG_NAME = "eld-link.log"
BUILD_LOG = "eldconv-build.log"
Runner = Callable[..., subprocess.CompletedProcess]

# flags eld's --help will not list verbatim but that are always accepted
_ALWAYS_KNOWN = {"-o", "-L", "-l", "-T", "-e", "-m", "-z", "-u", "-s", "-S", "-r", "-t", "-x", "-X",
                 "-O", "-h", "-q", "-y", "-M", "-N", "-n", "-E", "-R", "-(", "-)", "-Map", "-rpath",
                 "-rpath-link", "-soname", "-Bstatic", "-Bdynamic", "-Bsymbolic", "-Bsymbolic-functions",
                 "-shared", "-static", "-pie", "-plugin", "-plugin-opt", "-dynamic-linker", "-Ttext",
                 "-Tdata", "-Tbss", "-flavor", "-v", "-V", "-b", "-c", "-d", "-g", "-i"}


_TEMP_DIRS = {"/tmp", "/var/tmp", os.path.realpath(tempfile.gettempdir()), tempfile.gettempdir()}
TEMP_OBJ_RE = re.compile(r"^(cc|ccl)[A-Za-z0-9]{6,8}\.o$")  # gcc/clang driver temp objects
CRT_OBJ_RE = re.compile(r"^[A-Za-z]?crt[A-Za-z0-9_]*\.o$")      # crt1.o, Scrt1.o, crti.o, crtbeginS.o ...
TRYCOMPILE_MARKERS = ("CMakeScratch", "TryCompile", "CMakeTmp", "CMakeFiles/CheckLibrary",
                      "meson-private")


@dataclass
class LinkInvocation:
    cwd: str
    args: List[str]

    def inputs(self) -> List[str]:
        """Positional arguments, i.e. object files / archives to link."""
        return [a.name for a in parse_raw(self.args) if a.positional]

    def is_probe(self) -> bool:
        """Capability query from the compiler driver / CMake (`ld -v`, `ld --help`,
        `ld -m elf --push-state --pop-state`), not a real link: no output, no inputs.
        A real link may well carry -v, so the flags alone cannot decide this."""
        return self.output is None and not self.inputs()

    def is_trycompile(self) -> bool:
        """A configure-time probe link: CMake try_compile, an autoconf conftest,
        or a meson sanity check. Their outputs are deleted straight away."""
        if any(m in self.cwd for m in TRYCOMPILE_MARKERS):
            return True
        out = self.output
        if not out:
            # autoconf's "does the compiler work" links to the default a.out and deletes it
            return bool(self.inputs()) and not (Path(self.cwd) / "a.out").exists()
        if out == "/dev/null":
            return True     # `cc x.c -o /dev/null`: a does-this-link feature test
        if Path(out).name.startswith(("conftest", "sanitycheck")):
            return True
        # kbuild's `ld-option`/`cc-option` link into `mktemp` files straight under $TMPDIR
        # and delete them again
        op = self.output_path()
        if op is not None and str(op.parent) in _TEMP_DIRS and not is_elf(op):
            return True
        # A one-shot `cc probe.c -o have_x` (a Makefile-time feature test) links nothing
        # but the driver's own temporary objects. It may fail by design (that is what
        # the test is for); a real tool built the same way still has to produce an ELF.
        ins = [i for i in self.inputs() if not CRT_OBJ_RE.match(Path(i).name)]
        if ins and all(TEMP_OBJ_RE.match(Path(i).name) for i in ins):
            op = self.output_path()
            return op is None or not is_elf(op)
        return False

    @property
    def output(self) -> Optional[str]:
        for i, a in enumerate(self.args):
            if a == "-o" and i + 1 < len(self.args):
                return self.args[i + 1]
            if a.startswith("-o") and len(a) > 2 and not a.startswith("--"):
                return a[2:]
            if a.startswith("--output="):
                return a.split("=", 1)[1]
        return None

    def output_path(self) -> Optional[Path]:
        o = self.output
        if o is None:
            return None
        p = Path(o)
        return p if p.is_absolute() else Path(self.cwd) / p

    def option_names(self) -> List[str]:
        names = []
        for a in self.args:
            if not a.startswith("-") or a == "-":
                continue
            if a.startswith("--"):
                names.append(a.split("=", 1)[0])
            elif a[:2] in ("-l", "-L", "-O", "-z", "-m") and len(a) > 2:
                names.append(a[:2])
            else:
                names.append(a.split("=", 1)[0])
        return names


@dataclass
class CheckResult:
    ok: bool = False
    eld: Optional[str] = None
    eld_version: Optional[str] = None
    build_dir: Optional[str] = None
    commands: List[List[str]] = field(default_factory=list)
    returncode: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    invocations: int = 0        # real link steps
    probes: int = 0             # driver/CMake capability queries through the shim
    trycompiles: int = 0
    outputs: List[Dict] = field(default_factory=list)
    unknown_flags: List[str] = field(default_factory=list)
    ignored_flags: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tentative_confirmed: Dict[str, bool] = field(default_factory=dict)
    build_log: Optional[str] = None     # every command's full output
    reference: Optional[Dict] = None    # --compare: the original linker's build

    def as_dict(self) -> dict:
        return {"ok": self.ok, "eld": self.eld, "eld_version": self.eld_version,
                "build_dir": self.build_dir, "build_log": self.build_log, "reference": self.reference,
                "commands": self.commands, "returncode": self.returncode,
                "invocations": self.invocations, "probes": self.probes, "trycompiles": self.trycompiles,
                "outputs": self.outputs,
                "unknown_flags": self.unknown_flags, "ignored_flags": self.ignored_flags,
                "problems": self.problems, "warnings": self.warnings,
                "tentative_confirmed": self.tentative_confirmed,
                "stdout_tail": self.stdout_tail, "stderr_tail": self.stderr_tail}


# --------------------------------------------------------------------------- pieces

def find_eld(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        p = Path(explicit)
        return str(p.resolve()) if p.exists() else (shutil.which(explicit))
    for name in ("ld.eld", "eld"):
        w = shutil.which(name)
        if w:
            return w
    return None


def parse_link_log(path: Path) -> List[LinkInvocation]:
    invs: List[LinkInvocation] = []
    if not Path(path).exists():
        return invs
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if not parts:
            continue
        invs.append(LinkInvocation(parts[0], parts[1:]))
    return invs


def _tolerant(default):
    """A malformed ELF (truncated, absurd offsets) is simply "not understood", never a crash:
    the build tree may hold fuzz inputs, test fixtures and half-written files."""
    def wrap(fn):
        import functools

        @functools.wraps(fn)
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except (struct.error, OverflowError, IndexError, ValueError, MemoryError):
                return default() if callable(default) else default
        return inner
    return wrap


def is_elf(path: Path) -> bool:
    with open_elf(path) as elf:
        return elf is not None and elf.type in (1, 2, 3, 4)  # REL, EXEC, DYN, CORE


def elf_type(path: Path) -> Optional[str]:
    with open_elf(path) as elf:
        return elf.type_name if elf else None


@_tolerant(lambda: (None, ""))
def elf_notes_and_comment(path: Path) -> Tuple[Optional[str], str]:
    """(GNU build-id as hex, .comment text) of an ELF file; (None, "") if unreadable."""
    with open_elf(path) as elf:
        return (elf.build_id, elf.comment) if elf else (None, "")


# glibc's own bookkeeping symbols, which distro binaries list as undefined with no relocation
_GLIBC_INTERNAL_DATA = {"__progname", "__progname_full"}


@_tolerant(list)
def unresolved_data_symbols(path: Path) -> List[str]:
    """Undefined global *data* symbols of a dynamic ELF that no dynamic relocation fills in.

    A program referencing `stdout` or `environ` needs the linker to emit a copy (or GOT)
    relocation for it; if it does not, the symbol stays at address 0 / a bogus address and the
    program crashes on first use even though the link "succeeded" and the file is a perfectly
    valid ELF. Reading the symbol and relocation tables is enough to see it, with no need to
    run the binary (which may be a cross-compiled one).
    """
    with open_elf(path) as elf:
        return _unresolved_data_symbols(elf) if elf else []


def _unresolved_data_symbols(elf: ELF) -> List[str]:
    dyn = [i for i, sec in enumerate(elf.sections) if sec.type == SHT_DYNSYM]
    if not dyn:
        return []
    di = dyn[0]
    referenced = set()
    e, is64 = elf.endian, elf.is64
    for sec in elf.sections:
        if sec.type not in (SHT_RELA, SHT_REL) or sec.link != di:
            continue
        esz = sec.entsize or ((24 if is64 else 12) if sec.type == SHT_RELA else (16 if is64 else 8))
        end = min(sec.offset + sec.size, len(elf.data))
        for pos in range(sec.offset, end - esz + 1, esz):
            info, = struct.unpack_from(e + ("Q" if is64 else "I"), elf.data, pos + (8 if is64 else 4))
            referenced.add(info >> 32 if is64 else info >> 8)
    names = []
    for sym in elf.symbols(elf.sections[di]):
        if (sym.shndx != SHN_UNDEF or sym.bind not in (STB_GLOBAL, STB_WEAK)
                or sym.type not in (STT_OBJECT, STT_TLS) or sym.index in referenced):
            continue
        if sym.name not in _GLIBC_INTERNAL_DATA:
            names.append(sym.name)
    return names


_DYNREL_TYPES = (SHT_RELA, SHT_REL, SHT_RELR)


@_tolerant(list)
def empty_relocation_bounds(path: Path) -> List[Tuple[str, str, str, int]]:
    """(section, start symbol, end symbol, section size) for each non-empty dynamic-relocation
    section whose linker-script `X_start`/`X_end` symbol pair does not cover it.

    Kernels and boot loaders relocate themselves by walking the range between two symbols the
    linker script places around their dynamic relocations (`__pi_rela_start`/`__pi_rela_end` in
    an arm64 kernel, `__rela_dyn_start`/`__rela_dyn_end` in riscv). eld fills such a section with
    the relocations it generates but sets the end symbol as if the section were empty. The image
    links, and runs while it is not moved, but applies no relocation once it is: an arm64 kernel
    with KASLR dies before printing anything.
    """
    with open_elf(path) as elf:
        return _empty_relocation_bounds(elf) if elf else []


def _empty_relocation_bounds(elf: ELF) -> List[Tuple[str, str, str, int]]:
    relocs = {}   # start address -> (section name, size), allocated non-empty REL/RELA/RELR only
    for sec in elf.sections:
        if sec.type in _DYNREL_TYPES and sec.flags & SHF_ALLOC and sec.size:
            relocs[sec.addr] = (sec.name or "?", sec.size)
    symtab = next(iter(elf.of_type(SHT_SYMTAB)), None)
    if not relocs or symtab is None:
        return []
    # only symbols inside a relocation section's [start, end] can be either half of a pair
    spans = [(a, a + s) for a, (_, s) in relocs.items()]
    values: Dict[str, int] = {}
    for sym in elf.symbols(symtab):
        if sym.name and any(lo <= sym.value <= hi for lo, hi in spans):
            values[sym.name] = sym.value
    bad = []
    for start, value in sorted(values.items()):
        if not start.endswith("_start") or value not in relocs:
            continue
        end = start[:-len("_start")] + "_end"
        section, size = relocs[value]
        if end in values and values[end] != value + size:
            bad.append((section, start, end, size))
    return bad


def kconfig(dst: Path, build_dir: Optional[Path] = None) -> Dict[str, str]:
    """CONFIG_* values of a configured Linux kernel tree, built in place or with O=; {} otherwise."""
    if not (dst / "Kbuild").exists():
        return {}
    for d in (build_dir, dst):
        p = d / ".config" if d else None
        if p is not None and p.is_file():
            out = {}
            for line in p.read_text(errors="replace").splitlines():
                k, sep, v = line.partition("=")
                if sep and k.startswith("CONFIG_"):
                    out[k] = v
            return out
    return {}


# Other linkers' signatures in .comment (eld writes "Linker <ver> (<hash>)").
OTHER_LINKER_COMMENT_RE = re.compile(r"\bLLD\s+\d|\bmold\s+\d|\bGNU gold\b")
_SCAN_SKIP_DIRS = {".git", ".hg", ".svn", "eld-bin", "CMakeFiles", "__pycache__", "node_modules",
                   "meson-private", "meson-logs", "CMakeScratch"}


def linked_elsewhere(roots: List[Path], since: float, eld_outputs: Set[Path],
                     max_files: int = 200000) -> Tuple[List[str], List[str]]:
    """ELF executables/shared objects built after `since` that eld did not link.

    Returns (wrong_linker, unaccounted): files whose .comment names another
    linker, and files merely not seen in the shim log -- the latter can be
    legitimate (objcopy/strip of an eld output, a prebuilt copied in), so they
    are reported as warnings. A file sharing a build-id with an eld output is
    a derived copy and is not reported at all.
    """
    eld_ids = set()
    for o in eld_outputs:
        bid, _ = elf_notes_and_comment(o)
        if bid:
            eld_ids.add(bid)
    resolved = {o.resolve() for o in eld_outputs if o.exists()}
    wrong: List[str] = []
    unaccounted: List[str] = []
    seen = 0
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in _SCAN_SKIP_DIRS]
            for f in files:
                seen += 1
                if seen > max_files:
                    return wrong, unaccounted
                p = Path(d) / f
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime < since or st.st_size < 64:
                    continue
                if elf_type(p) not in ("EXEC", "DYN") or p.resolve() in resolved:
                    continue
                bid, comment = elf_notes_and_comment(p)
                if bid and bid in eld_ids:
                    continue
                (wrong if OTHER_LINKER_COMMENT_RE.search(comment) else unaccounted).append(str(p))
    return sorted(set(wrong)), sorted(set(unaccounted))


_HELP_OPT_RE = re.compile(r"(?<![\w-])(-{1,2}[A-Za-z(][\w\-()]*)")

# eld does not fail on an option it does not know: it prints a warning and
# links anyway, so the build succeeds with that flag silently doing nothing.
# The build output is therefore the authoritative source for "eld ignored this".
IGNORED_OPT_RE = re.compile(
    r"(?:unrecognized|unrecognised|unknown|ignoring unknown)\s+(?:option|argument)"
    r"\s*:?\s*[`'\"]?(-[^\s`'\"]*)", re.I)


# An eld built without ELD_ENABLE_SYMBOL_VERSIONING (the CMake default is OFF) skips every
# named version node with only a warning: the link "succeeds", the output has no symbol
# versions, and the nodes' `local:` hiding is lost too.
IGNORED_VERSION_SCRIPT_RE = re.compile(r"Warning:[^\n]*?Ignoring version script\s*[`'\"]?([^`'\"\n]+)")


def ignored_version_scripts(text: str) -> List[str]:
    return sorted({m.group(1).strip() for m in IGNORED_VERSION_SCRIPT_RE.finditer(text or "")})


def ignored_options(text: str) -> List[str]:
    """Options the linker reported as unknown while still succeeding."""
    return sorted({m.group(1) for m in IGNORED_OPT_RE.finditer(text or "")})


def parse_help(text: str) -> Set[str]:
    """Extract option names from `ld.eld --help` output."""
    names: Set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        for m in _HELP_OPT_RE.finditer(s.split("  ")[0]):
            names.add(m.group(1))
    return names


def probe_eld(eld: str, runner: Runner = subprocess.run) -> tuple:
    """Return (known option names, version string)."""
    known: Set[str] = set()
    version = None
    try:
        r = runner([eld, "--help"], capture_output=True, text=True, timeout=30)
        known = parse_help((r.stdout or "") + (r.stderr or ""))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        r = runner([eld, "--version"], capture_output=True, text=True, timeout=30)
        version = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0] if (r.stdout or r.stderr) else None
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return known, version


def emulations(invs: List["LinkInvocation"]) -> List[str]:
    """The `-m EMULATION` values the links used, in order of appearance."""
    seen: List[str] = []
    for inv in invs:
        args = inv.args
        for i, a in enumerate(args):
            if a == "-m" and i + 1 < len(args):
                emu = args[i + 1]
            elif a.startswith("-m") and len(a) > 2 and not a.startswith("-mllvm"):
                emu = a[2:]
            else:
                continue
            if emu not in seen:
                seen.append(emu)
    return seen


def target_help(eld: str, emus: List[str], runner: Runner = subprocess.run) -> Set[str]:
    """Options eld lists for these emulations. Its plain --help is the host driver's: ARM and
    AArch64 options (--pic-veneer, --fix-cortex-a53-843419) only show with `-m <emulation>`."""
    known: Set[str] = set()
    for emu in emus:
        try:
            r = runner([eld, "-m", emu, "--help"], capture_output=True, text=True, timeout=30)
            known |= parse_help((r.stdout or "") + (r.stderr or ""))
        except (OSError, subprocess.SubprocessError):
            continue
    return known


def unknown_flags(inv: LinkInvocation, known: Set[str]) -> List[str]:
    if not known:
        return []
    return sorted({n for n in inv.option_names() if n not in known and n not in _ALWAYS_KNOWN})


def detect_output_system(dst: Path, report: Optional[dict] = None) -> str:
    """Which build system an eldconv output uses: from its report, else by markers."""
    dst = Path(dst)
    sysname = ((report or {}).get("build") or {}).get("system")
    if sysname in ("cmake", "make", "gn", "ninja", "meson", "autotools"):
        return sysname
    if (dst / ARGS_GN_FILE).exists():
        return "gn"
    if (dst / TOOLCHAIN_FILE).exists():
        return "cmake"
    if (dst / "meson.build").exists():
        return "meson"
    if (dst / "configure.ac").exists() or (dst / "configure.in").exists():
        return "autotools"
    if (dst / "build.ninja").exists():
        return "ninja"
    if (dst / MK_FILE).exists():
        return "make"
    raise ValueError(f"{dst} does not look like an eldconv output "
                     f"(no {TOOLCHAIN_FILE}/{MK_FILE}/{ARGS_GN_FILE}/build.ninja/meson.build/configure.ac)")


def _safe_to_wipe(build_dir: Path, dst: Path) -> bool:
    """May `check` delete `build_dir` before a fresh configure?

    Only a directory inside the converted tree, or one that is recognisably a CMake/Meson build
    directory (and is not the tree itself or one of its ancestors). `--build-dir ..` or a
    directory holding someone's files must never be removed.
    """
    b, d = Path(os.path.abspath(build_dir)), Path(os.path.abspath(dst))
    if b == d or b in d.parents or b == Path(b.anchor):
        return False
    if d in b.parents:
        return True
    return (b / "CMakeCache.txt").is_file() or (b / "meson-private").is_dir()


def _split_build_args(build_args: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """Meson/Autotools: options and VAR=VAL go to the configure step, bare words
    (targets) to the build step."""
    conf, build = [], []
    for a in build_args or []:
        (conf if (a.startswith("-") or "=" in a) else build).append(a)
    return conf, build


def build_commands(dst: Path, build_dir: Path, eld: str, jobs: int, extra_cmake: List[str],
                   build_args: Optional[List[str]] = None, system: Optional[str] = None,
                   toolchain: str = "gnu") -> List[List[str]]:
    """Commands that build the converted tree.

    `build_args` are appended to the build step: a cross build needs the same
    variables the user would pass by hand (ARCH=arm64 CROSS_COMPILE=... for the
    kernel), and without them the build silently targets the host. For Meson
    and Autotools, options and VAR=VAL go to `meson setup` / `configure`
    instead, and bare words (targets) to the build.
    """
    dst = Path(dst)
    system = system or detect_output_system(dst)
    if system == "gn":
        # GN: args.gn goes in the out dir, then `gn gen` regenerates ninja files.
        out_rel = build_dir.relative_to(dst) if build_dir.is_relative_to(dst) else build_dir
        cmds: List[List[str]] = []
        if shutil.which("gn"):
            cmds.append(["gn", "gen", str(out_rel), f"--root={dst}"])
        cmds.append(["ninja", "-C", str(build_dir), f"-j{jobs}", *(build_args or [])])
        return cmds
    if system == "ninja":
        # Standalone ninja: the tree builds in place, the shim is already in
        # its link rules, so there is nothing to configure.
        return [["ninja", "-C", str(dst), f"-j{jobs}", *(build_args or [])]]
    if system == "cmake":
        return [
            ["cmake", "-S", str(dst), "-B", str(build_dir),
             f"-DCMAKE_TOOLCHAIN_FILE={dst / TOOLCHAIN_FILE}",
             "-DCMAKE_VERBOSE_MAKEFILE=ON", *extra_cmake],
            ["cmake", "--build", str(build_dir), f"-j{jobs}", *(build_args or [])],
        ]
    if system == "meson":
        conf, targets = _split_build_args(build_args)
        return [["meson", "setup", str(build_dir), str(dst), *conf],
                ["meson", "compile", "-C", str(build_dir), f"-j{jobs}", *targets]]
    if system == "autotools":
        conf, targets = _split_build_args(build_args)
        select = f"-B{dst / SHIM_DIR}/" + (" -fno-use-linker-plugin" if toolchain == "gnu" else "")
        user_ld = [a for a in conf if a.startswith("LDFLAGS=")]
        conf = [a for a in conf if not a.startswith("LDFLAGS=")]
        ldflags = select + "".join(" " + a[len("LDFLAGS="):] for a in user_ld)
        cmds = []
        if not (dst / "configure").exists():
            cmds.append(["autoreconf", "-fi"])
        from .converters.autotools import PATCH_LIBTOOL_SH
        cmds += [["./configure", f"LDFLAGS={ldflags}", *conf],
                 ["sh", "-c", PATCH_LIBTOOL_SH],
                 ["make", f"-j{jobs}", *targets]]
        return cmds
    return [["make", "-C", str(dst), f"ELD={eld}", f"-j{jobs}", *(build_args or [])]]


# --------------------------------------------------------------------------- driver

def default_jobs() -> int:
    return os.cpu_count() or 4


def _run_streaming(cmd: List[str], env: dict, cwd: str, timeout: Optional[int], sink, echo) \
        -> subprocess.CompletedProcess:
    """Run `cmd`, writing its output to `sink` as it comes (and each line to `echo`, if
    given) while collecting it like `subprocess.run(capture_output=True)` does.

    A kernel build runs for many minutes: `check` must not sit silent all that time, and a
    failure needs more than the tail it keeps in its report.
    """
    import signal
    import threading
    # its own process group, so that a timeout (or Ctrl-C) stops the whole build: killing
    # only `make` leaves its children running and holding the output pipes open
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                            text=True, errors="replace", bufsize=1, start_new_session=True)

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
    chunks: Dict[str, List[str]] = {"out": [], "err": []}
    lock = threading.Lock()

    def pump(stream, key):
        for line in stream:
            chunks[key].append(line)
            with lock:
                sink.write(line)
                if echo:
                    echo(line.rstrip("\n"))
        stream.close()

    threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group()
        for t in threads:
            t.join(5)
        raise subprocess.TimeoutExpired(cmd, timeout, "".join(chunks["out"]), "".join(chunks["err"]))
    except BaseException:
        kill_group()
        raise
    for t in threads:
        t.join()
    sink.flush()
    return subprocess.CompletedProcess(cmd, rc, "".join(chunks["out"]), "".join(chunks["err"]))


def _make_vars_only(build_args: Optional[List[str]]) -> List[str]:
    """The VAR=VAL subset of --build-arg, safe to also hand to `make clean`.

    A `--build-arg` may carry a make variable assignment (ARCH=arm64) or an
    actual target name (vmlinux) -- both are legitimate on the *build*
    command, but a target name has no business on `make clean`: appending it
    there makes "clean" *also* build that target, so the real link happens
    (and gets logged) during the clean phase, and the log is then deleted
    before the nominal build phase runs -- which finds nothing left to do and
    reports "eld ran but performed no link", even though the build that just
    ran genuinely linked everything with eld.
    """
    return [a for a in (build_args or []) if "=" in a and not a.startswith("-")]


def clean_commands(dst: Path, build_dir: Path, build_args: Optional[List[str]] = None,
                   system: Optional[str] = None) -> List[List[str]]:
    """Commands that force a relink, so a second `check` run still verifies something."""
    build_args = _make_vars_only(build_args)
    if system in ("cmake", "meson"):
        return []  # the build dir is removed outright
    if system == "autotools":
        return [["make", "clean"]] if (Path(dst) / "Makefile").exists() else []
    if (Path(dst) / MK_FILE).exists():
        return [["make", "-C", str(dst), "clean", *(build_args or [])]]
    if (Path(dst) / ARGS_GN_FILE).exists() and (build_dir / "build.ninja").exists():
        return [["ninja", "-C", str(build_dir), "-t", "clean"]]
    if (Path(dst) / "build.ninja").exists():
        return [["ninja", "-C", str(dst), "-t", "clean"]]
    return []  # cmake: the build dir is removed outright


REFERENCE_MARK = ".eldconv-reference"
REFERENCE_LOG = "eldconv-reference-build.log"


def reference_commands(ref: Path, ref_build: Path, system: str, jobs: int, extra_cmake: List[str],
                       build_args: List[str]) -> List[List[str]]:
    """What build_commands() runs, for the original tree and its own linker: no shim, no
    toolchain file, no eld."""
    if system == "gn":
        out_rel = ref_build.relative_to(ref) if ref_build.is_relative_to(ref) else ref_build
        return [["gn", "gen", str(out_rel), f"--root={ref}"],
                ["ninja", "-C", str(ref_build), f"-j{jobs}", *build_args]]
    if system == "ninja":
        return [["ninja", "-C", str(ref), f"-j{jobs}", *build_args]]
    if system == "cmake":
        return [["cmake", "-S", str(ref), "-B", str(ref_build), "-DCMAKE_VERBOSE_MAKEFILE=ON", *extra_cmake],
                ["cmake", "--build", str(ref_build), f"-j{jobs}", *build_args]]
    if system == "meson":
        conf, targets = _split_build_args(build_args)
        return [["meson", "setup", str(ref_build), str(ref), *conf],
                ["meson", "compile", "-C", str(ref_build), f"-j{jobs}", *targets]]
    if system == "autotools":
        conf, targets = _split_build_args(build_args)
        cmds = [] if (ref / "configure").exists() else [["autoreconf", "-fi"]]
        return cmds + [["./configure", *conf], ["make", f"-j{jobs}", *targets]]
    return [["make", "-C", str(ref), f"-j{jobs}", *build_args]]


def _compare_with_reference(res: CheckResult, report: dict, dst: Path, build_dir: Path, system: str,
                            jobs: int, extra_cmake: List[str], build_args: List[str],
                            pre_build: List[str], timeout: Optional[int], stream: bool, runner: Runner,
                            reference_dir: Optional[Path], log: Log) -> None:
    """Build the original tree with its own linker and compare each eld output with the file
    of the same name there."""
    from .elfcheck import compare
    from .generator import _copy_tree
    src = Path(report.get("source") or "")
    if not report.get("source") or not src.is_dir():
        res.problems.append("--compare: the source tree this was converted from is not recorded or no "
                            f"longer exists ({report.get('source') or 'no eldconv-report.json'})")
        return
    ref = Path(reference_dir) if reference_dir else dst.parent / f"{dst.name}.reference"
    ref = ref if ref.is_absolute() else Path.cwd() / ref
    if ref.exists() or ref.is_symlink():
        if not (ref / REFERENCE_MARK).is_file() or ref.is_symlink():
            res.problems.append(f"--compare: refusing to replace {ref}: it is not a reference build "
                                f"eldconv made (no {REFERENCE_MARK}); remove it or pass --reference-dir")
            return
        shutil.rmtree(ref)
    log.step(f"building the reference: {src} with its own linker, in {ref}")
    _copy_tree(src, ref, exclude_build_dirs=True)
    (ref / REFERENCE_MARK).write_text(f"reference build of {src} for {dst}\n")
    ref_build = ref / build_dir.relative_to(dst) if build_dir.is_relative_to(dst) else ref / "build-eld"
    ref_build.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ELDCONV_")}
    cmds = [["sh", "-c", c] for c in pre_build] + reference_commands(
        ref, ref_build, system, jobs, extra_cmake, build_args)
    ref_log = ref_build / REFERENCE_LOG
    problems: List[str] = []
    _, _, rc = _run_commands(cmds, env, ref, ref_log, stream, runner, timeout, problems, log)
    res.reference = {"dir": str(ref), "build_dir": str(ref_build), "build_log": str(ref_log),
                     "commands": cmds, "returncode": rc, "compared": 0}
    if rc != 0:
        res.problems.append(f"--compare: the reference build failed (rc={rc}"
                            + (f"; {problems[0]}" if problems else "") + f"); see {ref_log}")
        return
    log.step("comparing eld's outputs with the reference")
    for e in res.outputs:
        if e.get("trycompile") or not e.get("elf") or e.get("elf_type") not in ("EXEC", "DYN"):
            continue
        out = Path(e["output"])
        if build_dir != dst and out.is_relative_to(build_dir):
            refp = ref_build / out.relative_to(build_dir)
        elif out.is_relative_to(dst):
            refp = ref / out.relative_to(dst)
        else:
            continue
        with open_elf(refp) as r, open_elf(out) as n:
            if r is None or n is None:
                e["reference"] = None
                res.warnings.append(f"--compare: {out}: the reference build made no ELF file {refp}")
                continue
            findings = compare(r, n, (str(ref), str(dst)))
        res.reference["compared"] += 1
        e["reference"] = str(refp)
        e["differences"] = [f.as_dict() for f in findings]
        for f in findings:
            log.info(f"{out}: [{f.level}] {f.message}")
            if f.level != "info":
                (res.problems if f.level == "error" else res.warnings).append(
                    f"{out}: {f.message} (compared with {refp})")
    if not res.reference["compared"]:
        res.warnings.append("--compare: no eld output had a counterpart in the reference build")


def _run_commands(commands: List[List[str]], env: dict, cwd: Path, build_log: Path, stream: bool,
                  runner: Runner, timeout: Optional[int], problems: List[str], log: Log) \
        -> Tuple[str, str, int]:
    """Run `commands` in order until one fails, writing their output to `build_log`.
    Returns (stdout, stderr, exit status of the last one run)."""
    out, err, rc = "", "", 0
    echo = (lambda line: log.build_output(line)) if log.enabled else None
    with open(build_log, "w", errors="replace") as sink:
        for cmd in commands:
            log.command(cmd, str(cwd))
            sink.write(f"$ {' '.join(cmd)}\n")
            sink.flush()
            try:
                if stream:
                    r = _run_streaming(cmd, env, str(cwd), timeout, sink, echo)
                else:
                    kw = {"timeout": timeout} if timeout else {}
                    r = runner(cmd, capture_output=True, text=True, env=env, cwd=str(cwd), **kw)
                    sink.write((r.stdout or "") + (r.stderr or ""))
            except FileNotFoundError:
                problems.append(f"build tool not found: {cmd[0]} (install it or put it on PATH)")
                log.error(problems[-1])
                return out, err, 127
            except subprocess.TimeoutExpired as e:
                problems.append(f"timed out after {timeout}s: {' '.join(cmd)}")
                log.error(problems[-1])
                o = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
                r_err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
                if not stream:
                    sink.write(o + r_err)
                sink.write(f"[timed out after {timeout}s]\n")
                return out + o, err + r_err, 124
            log.info(f"exit {r.returncode}")
            sink.write(f"[exit {r.returncode}]\n")
            out += r.stdout or ""
            err += r.stderr or ""
            rc = r.returncode
            if rc != 0:
                break
    return out, err, rc


def run_check(dst: Path, eld: Optional[str] = None, build_dir: Optional[Path] = None,
              jobs: Optional[int] = None, dry_run: bool = False, extra_cmake: Optional[List[str]] = None,
              runner: Optional[Runner] = None, probe: bool = True, clean: bool = True,
              build_args: Optional[List[str]] = None, timeout: Optional[int] = None,
              pre_build: Optional[List[str]] = None, allow_unresolved_data: bool = False,
              allow_ignored_version_scripts: bool = False, compare: bool = False,
              reference_dir: Optional[Path] = None, log: Log = NULL_LOG) -> CheckResult:
    dst = Path(dst).resolve()
    jobs = jobs or default_jobs()
    # The build streams its output into a log file; a runner passed in (the tests' fakes)
    # is called like subprocess.run instead, and its output written to the log afterwards.
    stream = runner is None
    runner = runner or subprocess.run
    res = CheckResult()
    report = load_report(dst) or {}
    log.step(f"checking {dst}")
    eld_path = find_eld(eld or report.get("eld"))
    if eld_path is None:
        res.problems.append("ld.eld not found (pass --eld or put it on PATH)")
        return res
    res.eld = eld_path
    log.info(f"eld: {eld_path}")
    if build_dir:
        build_dir = Path(build_dir)
        if not build_dir.is_absolute():
            # A relative --build-dir is relative to the project (dst), not to
            # whatever directory the eldconv process itself happens to be run
            # from -- otherwise log_path/env/mkdir below silently operate on a
            # bogus path next to the CWD while ninja (run with cwd=dst) builds
            # in the *actually* intended directory under dst, and the shim's
            # ELDCONV_LINK_LOG never gets written where this function looks
            # for it.
            build_dir = dst / build_dir
    elif (dst / "build.ninja").exists() and not (dst / ARGS_GN_FILE).exists():
        build_dir = dst          # ninja builds in place
    else:
        build_dir = dst / "build-eld"
    res.build_dir = str(build_dir)
    log.info(f"build dir: {build_dir}")
    log_path = build_dir / LOG_NAME
    system = detect_output_system(dst, report)
    toolchain = ((report.get("build") or {}).get("toolchain")) or "gnu"
    res.commands = build_commands(dst, build_dir, eld_path, jobs, extra_cmake or [], build_args,
                                  system=system, toolchain=toolchain)
    res.commands = [["sh", "-c", c] for c in (pre_build or [])] + res.commands
    if dry_run:
        res.ok = True
        return res

    known: Set[str] = set()
    if probe:
        log.step("probing eld's option table")
        known, res.eld_version = probe_eld(eld_path, runner)
        log.info(f"version: {res.eld_version}")
        log.info(f"`--help` lists {len(known)} option(s)")
        if not known:
            log.warn("`ld.eld --help` produced no option list; flag validation skipped")

    env = dict(os.environ, ELDCONV_LINK_LOG=str(log_path), ELDCONV_ELD=eld_path)
    if system == "autotools":
        shim = str(dst / SHIM_DIR)
        env["COMPILER_PATH"] = shim + (":" + env["COMPILER_PATH"] if env.get("COMPILER_PATH") else "")
    if system == "meson":
        for k in ("CC_LD", "CXX_LD", "OBJC_LD", "OBJCXX_LD", "FC_LD"):
            env.pop(k, None)   # meson would hand these to the driver as -fuse-ld=
    if clean:
        # Without this an up-to-date tree relinks nothing and the check cannot
        # tell "eld linked everything" from "nothing was linked at all".
        for cmd in clean_commands(dst, build_dir, build_args, system):
            log.command(cmd, str(dst))
            try:
                runner(cmd, capture_output=True, text=True, env=env, cwd=str(dst),
                       **({"timeout": timeout} if timeout else {}))
            except (OSError, subprocess.SubprocessError) as e:
                log.warn(f"clean step failed ({e}); building anyway")
        if system in ("cmake", "meson") and build_dir.exists():
            if _safe_to_wipe(build_dir, dst):
                shutil.rmtree(build_dir)
            else:
                log.warn(f"not deleting {build_dir}: it is neither inside {dst} nor a CMake/Meson "
                         "build directory; stale configure state may be reused")
    build_dir.mkdir(parents=True, exist_ok=True)
    args_src = dst / ARGS_GN_FILE
    if args_src.exists():
        args_dst = build_dir / "args.gn"
        if not args_dst.exists() or args_src.read_text() not in args_dst.read_text():
            with open(args_dst, "a") as fh:
                fh.write(args_src.read_text())
    if log_path.exists():
        log_path.unlink()
    started = time.time() - 1
    log.step("building")
    build_log = build_dir / BUILD_LOG
    res.build_log = str(build_log)
    out, err, rc = _run_commands(res.commands, env, dst, build_log, stream, runner, timeout,
                                 res.problems, log)
    res.returncode = rc
    res.stdout_tail = out[-4000:]
    res.stderr_tail = err[-4000:]

    all_invs = parse_link_log(log_path)
    res.probes = sum(1 for i in all_invs if i.is_probe())
    invs = [i for i in all_invs if not i.is_probe()]
    res.trycompiles = sum(1 for i in invs if i.is_trycompile())
    log.step(f"verifying {len(all_invs)} shim invocation(s)")
    log.info(f"{res.probes} capability probe(s), {res.trycompiles} try_compile link(s)")
    if known:
        emus = emulations(invs)
        if emus:
            known = known | target_help(eld_path, emus, runner)
    for inv in invs:
        op = inv.output_path()
        entry: Dict = {"output": str(op) if op else None, "cwd": inv.cwd, "argc": len(inv.args)}
        if inv.is_trycompile():
            entry["trycompile"] = True
        if op is not None:
            entry["exists"] = op.exists()
            entry["elf"] = is_elf(op) if op.exists() else False
            entry["elf_type"] = elf_type(op) if op.exists() else None
        uf = unknown_flags(inv, known)
        if uf:
            entry["unknown_flags"] = uf
            if not inv.is_trycompile():   # a feature probe offers options on purpose
                res.unknown_flags = sorted(set(res.unknown_flags) | set(uf))
        res.outputs.append(entry)
        if not inv.is_trycompile():
            log.info(f"linked {entry.get('elf_type') or '?'} {entry.get('output')}"
                     + (f"  [unknown to eld --help: {' '.join(uf)}]" if uf else ""))
        else:
            log.detail(f"try_compile {entry.get('output')}")
    invs = [i for i in invs if not i.is_trycompile()]
    res.invocations = len(invs)

    # eld links on regardless, so these never show up as a build failure.
    res.ignored_flags = ignored_options(out + err)
    if res.ignored_flags:
        res.problems.append(
            "eld did not recognise, and silently ignored, these flags (the build "
            "succeeded without them): " + " ".join(res.ignored_flags))
        log.error(res.problems[-1])
    skipped = ignored_version_scripts(out + err)
    if skipped:
        msg = ("eld ignored the named version nodes of these version scripts, so the "
               "output has no symbol versions and their `local:` hiding is lost: "
               + " ".join(skipped) + " (this ld.eld was built without "
               "-DELD_ENABLE_SYMBOL_VERSIONING=ON; rebuild eld with it)")
        if allow_ignored_version_scripts:
            res.warnings.append(msg)
            log.warn(msg)
        else:
            res.problems.append(msg)
            log.error(msg)
    if rc != 0:
        res.problems.append(f"build failed (rc={rc})")
    if not invs:
        if rc != 0:
            # "up to date?" or "selection not applied" would send the user the wrong way
            res.problems.append("the build failed before any real link"
                                + (f" (eld did run for {len(all_invs)} configure-time probe link(s), so "
                                   "it is selected)" if all_invs else "")
                                + "; see stderr_tail")
        elif all_invs:
            res.problems.append("eld ran but performed no link (build was up to date?) - "
                                "re-run with a clean build directory")
        else:
            res.problems.append("no link step went through eld (shim log empty) - "
                                "linker selection not applied")
    # Anything linked that the shim never saw went through some other linker.
    if rc == 0 and invs:
        eld_outs = {Path(e["output"]) for e in res.outputs if e.get("output")}
        roots = [build_dir] if (build_dir.exists() and build_dir != dst
                                and system in ("cmake", "meson", "gn")) else [dst]
        wrong, other = linked_elsewhere(roots, started, eld_outs)
        if wrong:
            res.problems.append(f"{len(wrong)} binar{'y was' if len(wrong) == 1 else 'ies were'} "
                                f"linked by another linker: " + " ".join(wrong[:10]))
        if other:
            res.warnings.append(f"{len(other)} ELF file(s) were produced without going through "
                                f"eld (copied, stripped or objcopy'd outputs are expected here): "
                                + " ".join(other[:10]))
    for e in res.outputs:
        if e.get("elf") and not e.get("trycompile") and e.get("elf_type") in ("EXEC", "DYN"):
            bad = unresolved_data_symbols(Path(e["output"]))
            if bad:
                e["unresolved_data_symbols"] = bad
                msg = (f"{e['output']}: undefined data symbol(s) with no relocation ({', '.join(bad[:6])}): "
                       "the linker left them unresolved, so the program will crash when it uses them "
                       "(a linker limitation -- eld 24.0's x86_64 backend emits no copy relocations -- "
                       "not something the conversion can fix)")
                (res.warnings if allow_unresolved_data else res.problems).append(msg)
    kcfg = kconfig(dst, build_dir)
    for e in res.outputs:
        if not (e.get("elf") and not e.get("trycompile") and e.get("elf_type") in ("EXEC", "DYN")):
            continue
        for section, start, end, size in empty_relocation_bounds(Path(e["output"])):
            e.setdefault("empty_relocation_bounds", []).append([section, start, end, size])
            msg = (f"{e['output']}: {section} holds {size} bytes of dynamic relocations, but the linker "
                   f"script's {start}/{end} do not span them (eld set {end} as if the section were "
                   "empty), so code that relocates the image itself by walking that range applies "
                   "none of them")
            if not kcfg:
                res.problems.append(msg)
            elif kcfg.get("CONFIG_RANDOMIZE_BASE") == "y":
                res.problems.append(msg + ": with CONFIG_RANDOMIZE_BASE (KASLR) this kernel crashes at boot "
                                    "before printing anything; build it with `scripts/config --disable "
                                    "RANDOMIZE_BASE` (CONFIG_RELOCATABLE can stay on)")
            else:
                res.warnings.append(msg + ": harmless while CONFIG_RANDOMIZE_BASE is off, since the kernel is "
                                    "then never moved, but it will not boot with KASLR")
    for e in res.outputs:
        if e.get("trycompile"):
            # CMake deletes its try_compile scratch tree; only judge it if it survived
            if e.get("exists") and not e.get("elf") and Path(e["output"]).is_file():
                res.problems.append(f"try_compile output is not a valid ELF: {e['output']}")
            continue
        if e.get("output") and not e.get("elf"):
            res.problems.append(f"output is not a valid ELF: {e['output']}")
            continue
        with open_elf(e["output"]) as elf:
            found = validate(elf) if elf else []
        if found:
            e["validation"] = [f.as_dict() for f in found]
        for f in found:
            (res.problems if f.level == "error" else res.warnings).append(
                f"{e['output']}: {'malformed ELF: ' if f.level == 'error' else ''}{f.message}")
    if compare:
        if rc == 0 and invs:
            _compare_with_reference(res, report, dst, build_dir, system, jobs, extra_cmake or [],
                                    build_args or [], pre_build or [], timeout, stream, runner,
                                    reference_dir, log)
        else:
            res.warnings.append("--compare: skipped, the eld build did not link anything to compare")
    if res.unknown_flags:
        (res.warnings if rc == 0 else res.problems).append(
            "flags not listed by `ld.eld --help`: " + " ".join(res.unknown_flags))
    # tentative mappings: confirmed if eld lists them, or a successful build used them
    used = {n for inv in invs for n in inv.option_names()}
    for d in report.get("diagnostics", []):
        if "tentative" not in d.get("message", ""):
            continue
        name = d["flag"].split("=", 1)[0].split(" ", 1)[0]
        confirmed = name in known or name in _ALWAYS_KNOWN or (rc == 0 and name in used)
        res.tentative_confirmed[d["flag"]] = confirmed
        if not confirmed:
            res.warnings.append(f"tentative mapping not confirmed by eld: {d['flag']}")
    res.ok = not res.problems
    for w in res.warnings:
        log.warn(w)
    for pr in res.problems:
        log.error(pr)
    log.step("CHECK " + ("PASSED" if res.ok else "FAILED"))
    (build_dir / CHECK_REPORT).write_text(json.dumps(res.as_dict(), indent=2))
    return res
