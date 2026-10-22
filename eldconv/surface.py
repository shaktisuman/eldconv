"""What a particular ld.eld build actually accepts.

The rule table in mapping.py says what each bfd/gold/lld/mold option *means* for
eld. This module answers a narrower, empirical question: does this eld accept
the option at all? eld does not fail on an option it does not know -- it warns
("Unrecognized option") and links anyway, silently dropping the behaviour -- so
an option that is merely kept by the converter can still be lost.

A *surface* is a snapshot of that answer for one eld version, built by offering
every option bfd/gold/lld/mold know (plus eld's own) to the linker in a real
link, per target. eldconv ships the snapshot for the eld it was validated
against (data/eld-surface.json) and `eldconv surface --eld PATH` builds one for
any other eld.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

DATA = Path(__file__).parent / "data"
BUNDLED = DATA / "eld-surface.json"
SOURCE_OPTIONS = DATA / "source_options.json"

# clang triples for the targets eld supports. i386 was absent while eld could not link
# it at all ("Emulation failed for target : i386"); eld's 32-bit x86 backend landed in
# late September 2026, so it is probed now. A target whose probe objects fail to link
# is simply left out of a snapshot, which is how an older eld shows up as "no i386".
TARGETS: Dict[str, Tuple[str, ...]] = {
    "x86_64": ("x86_64-linux-gnu",),
    "aarch64": ("aarch64-linux-gnu",),
    "arm": ("armv7a-linux-gnueabihf", "-mfloat-abi=hard"),
    "riscv64": ("riscv64-linux-gnu",),
    "riscv32": ("riscv32-unknown-elf",),
    "i386": ("i386-linux-gnu",),
    "hexagon": ("hexagon-unknown-linux-musl", "-mv68"),
}
UNRECOGNIZED = re.compile(r"unrecogni[sz]ed option|unknown option|unknown argument", re.I)


@dataclass
class Surface:
    eld_version: str = ""
    targets: List[str] = field(default_factory=list)
    options: Dict[str, List[str]] = field(default_factory=dict)   # name -> targets accepting
    z: Dict[str, List[str]] = field(default_factory=dict)         # -z keyword -> targets

    # -- lookup --------------------------------------------------------------
    def accepting(self, name: str, value: Optional[str] = None) -> Optional[List[str]]:
        """The targets eld accepts this option for; None if it was never probed."""
        if name == "-z":
            if not value:
                return None
            return self.z.get(value.split("=", 1)[0])
        return self.options.get(name)

    def accepts(self, name: str, value: Optional[str] = None,
                target: Optional[str] = None) -> Optional[bool]:
        """True/False if this surface knows; None if the option was never probed.

        Without a target (or for one this snapshot did not probe), an option counts
        as accepted if eld takes it for *any* target: eld's ARM/AArch64 driver knows
        options its x86-64 driver does not, and which one a build uses depends on
        its inputs.
        """
        hit = self.accepting(name, value)
        if hit is None:
            return None
        if target and target in self.targets:
            return target in hit
        return bool(hit)

    def as_dict(self) -> dict:
        return {"eld_version": self.eld_version, "targets": self.targets,
                "options": dict(sorted(self.options.items())), "z": dict(sorted(self.z.items()))}

    @classmethod
    def from_dict(cls, d: dict) -> "Surface":
        """Raises ValueError for anything that is not a snapshot (a hand-edited or
        truncated file must not turn into "eld accepts nothing" or a traceback)."""
        if not isinstance(d, dict):
            raise ValueError("not an eld surface snapshot: expected a JSON object")
        targets, options, z = d.get("targets", []), d.get("options", {}), d.get("z", {})
        if not (isinstance(targets, list) and all(isinstance(t, str) for t in targets)):
            raise ValueError("eld surface snapshot: 'targets' must be a list of strings")
        for key, table in (("options", options), ("z", z)):
            if not (isinstance(table, dict) and all(isinstance(v, list) for v in table.values())):
                raise ValueError(f"eld surface snapshot: '{key}' must map names to target lists")
        return cls(str(d.get("eld_version", "")), list(targets), dict(options), dict(z))

    @classmethod
    def load(cls, path: Path) -> "Surface":
        s = cls.from_dict(json.loads(Path(path).read_text()))
        if not s.targets or not (s.options or s.z):
            raise ValueError("eld surface snapshot is empty")
        return s


def bundled() -> Optional[Surface]:
    try:
        return Surface.load(BUNDLED)
    except (OSError, ValueError):
        return None


# Options the bundled snapshot knows eld accepts that older eld builds do not have. If a
# given ld.eld lacks them in its --help the snapshot is for a newer eld than the one in use.
CANARIES = ("--no-merge-constants", "--undefined-version", "--replay")


def stale_snapshot(eld: str, runner: Callable = subprocess.run) -> List[str]:
    """Canary options the bundled snapshot assumes but this ld.eld does not list in --help."""
    try:
        r = runner([eld, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    text = (r.stdout or "") + (r.stderr or "")
    if not text.strip():
        return []
    return [c for c in CANARIES if c not in text]


def _identity(eld: str, runner: Callable) -> str:
    """What `--version` and `--help` print. ld.eld is often a wrapper script, whose own
    size and mtime do not change when the binary behind it is rebuilt; its output does."""
    out = []
    for arg in ("--version", "--help"):
        try:
            r = runner([eld, arg], capture_output=True, text=True, timeout=30)
            out.append((r.stdout or "") + (r.stderr or ""))
        except (OSError, subprocess.SubprocessError):
            out.append("")
    return "\0".join(out)


def cache_file(eld: str, runner: Callable = subprocess.run) -> Path:
    """Where the probed snapshot of this exact ld.eld is cached: keyed on its path, size,
    mtime and what it prints for --version/--help."""
    import hashlib
    real = os.path.realpath(eld)
    st = os.stat(real)
    # which clang probes the targets: without one only the host target can be probed,
    # and installing clang later must not keep serving that partial snapshot
    ident = f"{real}|{st.st_size}|{st.st_mtime_ns}|{shutil.which('clang')}|{_identity(real, runner)}"
    key = hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:16]
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(root) / "eldconv" / f"surface-{key}.json"


def cached_or_build(eld: str, progress: Optional[Callable[[int, int], None]] = None,
                    jobs: int = 4) -> "Surface":
    """The snapshot for this ld.eld: from the cache, else probed now (a minute or two) and cached."""
    f = cache_file(eld)
    try:
        return Surface.load(f)
    except (OSError, ValueError):
        pass
    s = build(eld, jobs=jobs, progress=progress)
    try:                     # atomically: a parallel eldconv must never read half a file
        f.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(f.parent), prefix=".surface-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(s.as_dict(), fh, indent=1)
            os.replace(tmp, f)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError:
        pass
    return s


# The surface conversions consult. A context variable rather than a parameter
# threaded through every converter: the snapshot is process-wide configuration,
# and tests can swap it with use_surface().
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("eldconv_surface", default="bundled")


def active() -> Optional[Surface]:
    s = _ACTIVE.get()
    if s == "bundled":
        s = bundled()
        _ACTIVE.set(s)
    return s


@contextmanager
def use_surface(s: Optional[Surface]) -> Iterator[None]:
    tok = _ACTIVE.set(s)
    try:
        yield
    finally:
        _ACTIVE.reset(tok)


def set_active(s: Optional[Surface]) -> None:
    _ACTIVE.set(s)


# ----------------------------------------------------------------------- targets

# The target a conversion is for (`--target`), or None for "any target eld has". Only
# 13 options in the bundled snapshot depend on it (ARM errata, --pic-veneer, --relax,
# -G, ...), but without it an option eld's x86-64 driver ignores is kept in an x86-64
# conversion because the AArch64 driver knows it.
_TARGET: contextvars.ContextVar = contextvars.ContextVar("eldconv_target", default=None)

_TARGET_ALIASES = {"arm64": "aarch64", "amd64": "x86_64", "x86-64": "x86_64", "x64": "x86_64",
                   "armhf": "arm", "armel": "arm", "x86": None, "riscv": None}

# bfd/eld emulation names (`-m EMULATION`) by target
_EMULATIONS = (
    (re.compile(r"elf(?:32)?_x86_64"), "x86_64"),
    (re.compile(r"elf_i386|elf_iamcu"), "i386"),
    (re.compile(r"aarch64"), "aarch64"),
    (re.compile(r"arm"), "arm"),
    (re.compile(r"elf64[lb]riscv"), "riscv64"),
    (re.compile(r"elf32[lb]riscv"), "riscv32"),
    (re.compile(r"hexagon"), "hexagon"),
)


def normalize_target(name: str) -> Optional[str]:
    """`aarch64`, `arm64`, `aarch64-linux-gnu`, `i686`, `armv7a` -> a TARGETS key; None
    if it is not a target eld has (or, like the kernel's `x86`, names more than one)."""
    t = name.strip().lower()
    if t in TARGETS:
        return t
    if t in _TARGET_ALIASES:
        return _TARGET_ALIASES[t]
    arch = t.split("-", 1)[0]
    if arch != t:                                   # a triple
        return normalize_target(arch)
    if re.fullmatch(r"i[3-6]86", t):
        return "i386"
    if re.fullmatch(r"armv\d+\w*|thumbv\d+\w*", t):
        return "arm"
    return None


def emulation_target(emu: str) -> Optional[str]:
    """The target of a `-m EMULATION` value (`aarch64linux` -> aarch64), or None."""
    for rx, t in _EMULATIONS:
        if rx.match(emu):
            return t
    return None


def active_target() -> Optional[str]:
    return _TARGET.get()


def set_target(t: Optional[str]) -> None:
    _TARGET.set(t)


@contextmanager
def use_target(t: Optional[str]) -> Iterator[None]:
    tok = _TARGET.set(t)
    try:
        yield
    finally:
        _TARGET.reset(tok)


# --------------------------------------------------------------------- building

def source_options() -> Tuple[Set[str], Set[str]]:
    d = json.loads(SOURCE_OPTIONS.read_text())
    opts = {o for v in d["options"].values() for o in v}
    zs = {z for v in d["z"].values() for z in v}
    return opts, zs


def _objects(work: Path, runner: Callable) -> Dict[str, Tuple[Path, Path]]:
    """Two tiny objects per target (distinct symbols, so both can be linked)."""
    (work / "a.c").write_text("int eldconv_probe_a(void){return 0;}\n")
    (work / "b.c").write_text("int eldconv_probe_b(void){return 1;}\n")
    objs: Dict[str, Tuple[Path, Path]] = {}
    clang = shutil.which("clang")
    for tgt, (triple, *extra) in TARGETS.items():
        pair = []
        for src in ("a", "b"):
            out = work / f"{tgt}-{src}.o"
            cmds = [[clang, f"--target={triple}", *extra, "-c", str(work / f"{src}.c"), "-o", str(out)]] \
                if clang else []
            if tgt == "x86_64":
                cmds.append(["cc", "-c", str(work / f"{src}.c"), "-o", str(out)])
            for cmd in cmds:
                try:
                    r = runner(cmd, capture_output=True, text=True, timeout=60)
                except (OSError, subprocess.SubprocessError):
                    continue
                if r.returncode == 0 and out.exists():
                    pair.append(out)
                    break
        if len(pair) == 2:
            objs[tgt] = (pair[0], pair[1])
    return objs


def _rejected(eld: str, argv: List[str], obj: Path, work: Path, runner: Callable) -> Optional[bool]:
    d = tempfile.mkdtemp(dir=work)
    try:
        r = runner([eld, *argv, "-o", os.path.join(d, "out"), str(obj)],
                   capture_output=True, text=True, timeout=30, cwd=d)
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(UNRECOGNIZED.search((r.stdout or "") + (r.stderr or "")))


def _links_at_all(eld: str, obj: Path, work: Path, runner: Callable) -> bool:
    """Can eld link this target's probe object without any option? A target eld cannot
    link (i386 on an older eld) must not count as "accepting" every option just because
    nothing there ever says "unrecognized"."""
    d = tempfile.mkdtemp(dir=work)
    try:
        r = runner([eld, "-o", os.path.join(d, "out"), str(obj)],
                   capture_output=True, text=True, timeout=30, cwd=d)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def probe_option(eld: str, name: str, objs: Dict[str, Tuple[Path, Path]], work: Path,
                 runner: Callable, z: bool = False) -> List[str]:
    """Targets for which eld accepts `name` in some spelling.

    eld only reports unknown options once it gets as far as linking, so every
    variant must be a link that would otherwise succeed: a value-taking option
    is given a real second object as its value, never a nonexistent file.
    """
    ok: List[str] = []
    for tgt, (a, b) in objs.items():
        if z:
            variants = [["-z", name], ["-z", name + "=1"], ["-z", name + "=4096"]]
        else:
            second = Path(tempfile.mkdtemp(dir=work)) / "second.o"
            shutil.copy(b, second)
            variants = [[name], [name + "=1"], [name, str(second)], [name + "1"]]
        for v in variants:
            if _rejected(eld, v, a, work, runner) is False:
                ok.append(tgt)
                break
    return ok


def eld_version(eld: str, runner: Callable = subprocess.run) -> str:
    try:
        r = runner([eld, "--version"], capture_output=True, text=True, timeout=30)
        return ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def build(eld: str, extra_options: Iterable[str] = (), extra_z: Iterable[str] = (),
          runner: Callable = subprocess.run, jobs: int = 8,
          progress: Optional[Callable[[int, int], None]] = None) -> Surface:
    """Probe `eld` with every option eldconv might ever hand it."""
    from .checker import parse_help
    from .mapping import RULES
    opts, zs = source_options()
    help_text = ""
    try:
        r = runner([eld, "--help"], capture_output=True, text=True, timeout=30)
        help_text = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        pass
    opts |= {o for o in parse_help(help_text) if len(o) > 1 and "=" not in o}
    zs |= set(re.findall(r"-z[= ]([a-z][a-z0-9-]*[a-z0-9])", help_text))
    for rule in RULES:
        if not any(c in rule.name for c in "*?["):
            if rule.name == "-z":
                if rule.value and not any(c in rule.value for c in "*?["):
                    zs.add(rule.value.split("=", 1)[0])
            else:
                opts.add(rule.name)
        if rule.target and not rule.target.startswith("-z"):
            opts.add(re.split(r"[= ]", rule.target, maxsplit=1)[0])
    opts |= set(extra_options)
    zs |= set(extra_z)
    opts -= {"-o", "--output", "--help", "-h", "--version", "-v", "-V", "-z", "-(", "-)"}

    work = Path(tempfile.mkdtemp(prefix="eldconv-surface-"))
    try:
        objs = _objects(work, runner)
        if not objs:
            raise RuntimeError("no C compiler could build a probe object (need clang or cc)")
        objs = {t: ab for t, ab in objs.items() if _links_at_all(eld, ab[0], work, runner)}
        if not objs:
            raise RuntimeError("eld could not link a probe object for any target")
        jobs_list = [(o, False) for o in sorted(opts)] + [(k, True) for k in sorted(zs)]
        done = [0]

        def one(job):
            name, is_z = job
            res = probe_option(eld, name, objs, work, runner, z=is_z)
            done[0] += 1
            if progress:
                progress(done[0], len(jobs_list))
            return job, res

        with ThreadPoolExecutor(max(1, jobs)) as ex:
            results = list(ex.map(one, jobs_list))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    s = Surface(eld_version(eld, runner), sorted(objs))
    for (name, is_z), res in results:
        (s.z if is_z else s.options)[name] = res
    return s
