"""Audit the mapping table against a real ld.eld.

The rule table in mapping.py is best-effort: it was written from eld's public
option surface, and every rule marked `tentative` is a guess. Given an actual
ld.eld binary this compares the table with what the linker really accepts and
reports the three ways they can disagree:

  missing    a KEEP/RENAME rule names an option eld does not list -- the build
             will fail on it
  needless   a DROP/UNSUPPORTED rule throws away an option eld *does* support --
             the conversion is losing behaviour for no reason
  confirmed  a tentative rule that the probe agrees with

Options are also checked by *acceptance*, not just by `--help`: eld may accept
options it does not advertise, so each candidate is offered to the linker in a
trivial link and judged by whether it complains.
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .checker import _ALWAYS_KNOWN, probe_eld
from .log import NULL_LOG, Log
from .mapping import RULES, Action

# Rules whose "name" is a glob or a driver-level concern rather than a linker
# option; there is nothing to look up for these.
SKIP_NAMES = {"-fuse-ld", "--ld-path"}

# DROP rules kept on purpose even though a current eld accepts the option: dropping is
# harmless and works with every eld build, keeping it would warn on older ones.
INTENTIONAL_DROPS = {"-O"}


@dataclass
class Finding:
    kind: str            # missing | needless | confirmed | unknown
    option: str
    action: str
    tentative: bool
    linkers: List[str]
    note: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "option": self.option, "action": self.action,
                "tentative": self.tentative, "linkers": self.linkers, "note": self.note}


@dataclass
class AuditResult:
    eld: str
    version: Optional[str] = None
    listed: int = 0
    findings: List[Finding] = field(default_factory=list)
    accepted: Dict[str, bool] = field(default_factory=dict)

    def by_kind(self, kind: str) -> List[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def as_dict(self) -> dict:
        return {"eld": self.eld, "version": self.version, "options_listed": self.listed,
                "summary": {k: len(self.by_kind(k))
                            for k in ("missing", "needless", "confirmed", "unknown")},
                "findings": [f.as_dict() for f in self.findings]}


# eld reports an option it does not know and then *carries on*, so neither the
# exit status nor `--version` can be used to test acceptance: the option must be
# offered during an actual link and the output read.
UNKNOWN_MARKERS = ("unrecognized option", "unrecognised option", "unknown option",
                   "unknown argument", "invalid option", "unsupported option",
                   "no such option", "ignoring unknown")


def _make_probe_object(workdir: Path, runner) -> Optional[Path]:
    """A minimal object file to link, so options are actually parsed."""
    obj = workdir / "probe.o"
    if obj.exists():
        return obj
    src = workdir / "probe.c"
    src.write_text("int _eldconv_probe(void){return 0;}\n")
    for cc in ("cc", "gcc", "clang"):
        try:
            r = runner([cc, "-c", str(src), "-o", str(obj)],
                       capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and obj.exists():
            return obj
    return None


def _probe_accepts(eld: str, option: str, workdir: Path,
                   runner=None, obj: Optional[Path] = None,
                   value: Optional[str] = None) -> Optional[bool]:
    """Offer one option (optionally with a concrete value) to eld in a real
    link and see whether it knows it.

    `-z <value>`-style keyed options are meaningless to probe by flag name
    alone -- eld may accept some keywords and reject others -- so when
    `value` is given it is passed as a second token (GNU `-z`/`-Wl,-z,`
    convention), matching how such flags are actually spelled on argv.

    The link itself may fail for unrelated reasons; only a complaint naming
    the option counts. Returns None when that cannot be told apart.
    """
    runner = runner or subprocess.run
    argv = [eld, option] + ([value] if value is not None else [])
    if obj is not None:
        argv += ["-o", str(workdir / "probe.out"), str(obj)]
    else:
        argv += ["--version"]
    try:
        r = runner(argv, capture_output=True, text=True, timeout=30, cwd=str(workdir))
    except (OSError, subprocess.SubprocessError):
        return None
    out = ((r.stdout or "") + (r.stderr or "")).lower()
    name = (value or option).lstrip("-").lower()
    for marker in UNKNOWN_MARKERS:
        if marker in out and (name in out or marker in out or option.lstrip("-").lower() in out):
            return False
    if obj is None and r.returncode != 0:
        return None
    return True


def _other_targets(eld: str, name: str, workdir: Path, runner, cross: Dict[str, List[str]]) -> List[str]:
    """Targets other than the host for which eld accepts `name` (needs clang). `cross`
    caches answers per option, and the probe objects under the key "" (built once)."""
    from . import surface
    if name not in cross:
        if "" not in cross:
            sub = workdir / "cross"
            sub.mkdir(exist_ok=True)
            objs = surface._objects(sub, runner)
            cross[""] = {t: ab for t, ab in objs.items()          # type: ignore[assignment]
                         if t != "x86_64" and surface._links_at_all(eld, ab[0], sub, runner)}
        cross[name] = surface.probe_option(eld, name, cross[""], workdir, runner)  # type: ignore[arg-type]
    return cross[name]


def audit(eld: str, probe_acceptance: bool = True, runner=None,
          log: Log = NULL_LOG) -> AuditResult:
    runner = runner or subprocess.run
    log.step(f"auditing the mapping table against {eld}")
    known, version = probe_eld(eld, runner)
    res = AuditResult(eld=eld, version=version, listed=len(known))
    log.info(f"version: {version}")
    log.info(f"`--help` lists {len(known)} option(s)")
    if not known:
        log.warn("eld listed no options; nothing to compare against")
        return res

    seen: Set[Tuple[str, Optional[str]]] = set()
    cross: Dict[str, List[str]] = {}     # see _other_targets
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        probe_obj = _make_probe_object(workdir, runner) if probe_acceptance else None
        if probe_acceptance and probe_obj is None:
            log.warn("no C compiler to build a probe object; acceptance testing is "
                     "limited to what `--help` lists")
        for rule in RULES:
            name = rule.name
            if name in SKIP_NAMES or any(c in name for c in "*?[") or rule.passthrough:
                continue          # passthrough: not meant for eld at all (PE/COFF options)
            if name in INTENTIONAL_DROPS and rule.action not in (Action.KEEP, Action.RENAME):
                continue
            # `-z <keyword>`-style rules key on (name, value): a bare `-z` being
            # in --help says nothing about which keywords eld accepts, so each
            # keyword needs its own probe rather than collapsing onto the first
            # rule seen for that flag name.
            keyed = rule.value is not None and not any(c in rule.value for c in "*?[")
            dedup_key = (name, rule.value if keyed else None)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if rule.value is not None and not keyed:
                continue  # glob-valued rule (e.g. "cet-report*"): no concrete value to probe

            label = f"{name} {rule.value}" if keyed else name
            accepts: Optional[bool] = None
            if keyed:
                # The specific keyword, not the bare flag, is what's in question.
                listed = False
                if probe_acceptance:
                    accepts = _probe_accepts(eld, name, workdir, runner, probe_obj,
                                             value=rule.value)
                    res.accepted[label] = bool(accepts)
            else:
                listed = name in known or name in _ALWAYS_KNOWN
                if probe_acceptance and not listed:
                    accepts = _probe_accepts(eld, name, workdir, runner, probe_obj)
                    res.accepted[name] = bool(accepts)
            supported = listed or accepts is True
            name = label  # findings below report the fully-keyed spelling

            linkers = sorted(rule.linkers)
            if rule.action in (Action.KEEP, Action.RENAME):
                target = rule.target or rule.name
                tgt_listed = (target.split("=")[0] in known
                              or target.split("=")[0] in _ALWAYS_KNOWN)
                if rule.action is Action.RENAME and not tgt_listed:
                    res.findings.append(Finding(
                        "missing", f"{name} -> {target}", rule.action.value,
                        rule.tentative, linkers,
                        "rename target is not accepted by this eld"))
                elif rule.action is Action.KEEP and not supported and not keyed and probe_acceptance \
                        and _other_targets(eld, rule.name, workdir, runner, cross):
                    # eld's ARM/AArch64 drivers know options its host driver rejects
                    # (--pic-veneer, --fix-cortex-a53-843419, --merge-exidx-entries)
                    res.findings.append(Finding(
                        "confirmed", name, rule.action.value, rule.tentative, linkers,
                        "accepted for " + ", ".join(cross[rule.name]) + " only"))
                elif rule.action is Action.KEEP and not supported:
                    res.findings.append(Finding(
                        "missing", name, rule.action.value, rule.tentative, linkers,
                        "kept by the table but this eld does not accept it"))
                elif rule.tentative:
                    res.findings.append(Finding(
                        "confirmed", name, rule.action.value, True, linkers,
                        "tentative rule confirmed by the probe"))
            else:  # DROP / UNSUPPORTED
                if supported:
                    res.findings.append(Finding(
                        "needless", name, rule.action.value, rule.tentative, linkers,
                        "dropped by the table but this eld accepts it"))
                elif rule.tentative:
                    res.findings.append(Finding(
                        "confirmed", name, rule.action.value, True, linkers,
                        "tentative rule confirmed by the probe"))

    # Options eld offers that the table has never heard of.
    covered = {r.name for r in RULES}
    for opt in sorted(known):
        if opt in covered or opt in SKIP_NAMES:
            continue
        if any(fnmatchcase(opt, r.name) for r in RULES if any(c in r.name for c in "*?[")):
            continue
        res.findings.append(Finding("unknown", opt, "-", False, [],
                                    "eld supports this; the table has no rule for it"))

    for kind in ("missing", "needless"):
        for f in res.by_kind(kind):
            log.warn(f"{kind}: {f.option} ({f.action}) -- {f.note}")
    log.info(f"confirmed {len(res.by_kind('confirmed'))} tentative rule(s)")
    log.info(f"{len(res.by_kind('unknown'))} eld option(s) not covered by the table")
    return res
