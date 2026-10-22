"""eldconv command line.

  eldconv detect  SRC
  eldconv convert SRC DST [--eld PATH] [--source-linker bfd|lld|gold|mold] [--target ARCH] [--check] [--strict]
  eldconv check   DST [--eld PATH] [--build-dir DIR] [--build-arg ARG] [--dry-run]
  eldconv flags   "-Wl,--icf=all -Wl,--gc-sections" [--source-linker lld] [--target ARCH] [--raw]
  eldconv audit   [--eld PATH]
  eldconv compare REFERENCE OUTPUT
  eldconv surface --eld PATH [-o FILE]

Exit status: 0 success; 1 check/audit failed, or `convert --strict` produced
errors; 2 usage or input error; 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import List

from . import LINKERS, __version__
from .audit import audit
from .checker import default_jobs, run_check
from .log import Log
from .detect import SYSTEMS, detect
from .generator import convert_project
from .rewrite import convert_driver_string, convert_raw_string


def _print_diags(diags) -> None:
    for d in diags:
        loc = (f"{d.file}:{d.line}: " if d.line else f"{d.file}: ") if d.file else ""
        print(f"[{d.level}] {loc}{d.flag}: {d.message}", file=sys.stderr)


def _log(a) -> Log:
    return Log(level=min(getattr(a, "verbose", 0) or 0, 2))


def _apply_surface(a) -> None:
    """--eld-surface FILE|auto|bundled|none selects what eld is assumed to accept."""
    from . import surface
    from .checker import find_eld
    choice = getattr(a, "eld_surface", None)
    eld_arg = getattr(a, "eld", None)
    if choice == "auto":
        eld = find_eld(eld_arg)
        if eld is None:
            raise ValueError("--eld-surface auto needs an ld.eld (pass --eld or put it on PATH)")
        cached = surface.cache_file(eld).exists()
        if not cached:
            print(f"eldconv: probing {eld} (one-off, a minute or two; cached afterwards)", file=sys.stderr)
        s = surface.cached_or_build(eld)
        print(f"eldconv: eld option snapshot for {eld}: targets {', '.join(s.targets)}", file=sys.stderr)
        surface.set_active(s)
        return
    if choice in (None, "bundled"):
        if eld_arg:
            eld = find_eld(eld_arg)
            missing = surface.stale_snapshot(eld) if eld else []
            if missing:
                print("[warn] the bundled eld option snapshot is newer than this ld.eld (its --help lacks "
                      + ", ".join(missing) + "); options only newer eld versions accept may be kept. "
                      "Use --eld-surface auto, or `eldconv surface` and --eld-surface FILE.",
                      file=sys.stderr)
        return
    if choice == "none":
        surface.set_active(None)
        return
    try:
        surface.set_active(surface.Surface.load(Path(choice)))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read eld surface {choice}: {e}")


def _apply_target(a) -> None:
    """--target ARCH: check options against what eld accepts for that target only."""
    from . import surface
    name = getattr(a, "target", None)
    if not name:
        surface.set_target(None)
        return
    t = surface.normalize_target(name)
    if t is None:
        why = "; the kernel's x86 is both i386 and x86_64" if name.lower() == "x86" else ""
        raise ValueError(f"--target {name}: not a target eld has (one of {', '.join(surface.TARGETS)}, "
                         f"an alias such as arm64, or a triple such as aarch64-linux-gnu{why})")
    s = surface.active()
    if s is not None and t not in s.targets:
        print(f"[warn] the eld option snapshot has no {t} (targets: {', '.join(s.targets)}); "
              "options are checked against every target it has", file=sys.stderr)
    surface.set_target(t)


def cmd_detect(a) -> int:
    info = detect(Path(a.src), a.source_linker, log=_log(a), system=a.build_system)
    print(json.dumps(info.as_dict(), indent=2))
    return 0


def cmd_convert(a) -> int:
    log = _log(a)
    _apply_surface(a)
    _apply_target(a)
    rep = convert_project(Path(a.src), Path(a.dst), eld=a.eld, source_linker=a.source_linker,
                          original_toolchain=a.toolchain_file, extra_cmake_args=" ".join(shlex.quote(x) for x in (a.cmake_arg or [])),
                          force=a.force, log=log, build_system=a.build_system)
    if not a.quiet and not log.enabled:
        # verbose mode already streamed these as they happened
        _print_diags(rep.diagnostics)
    print(f"converted {a.src} -> {a.dst}: {len(rep.files_rewritten)} file(s) rewritten, "
          f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s); see {a.dst}/eldconv-report.md")
    rc = 1 if (a.strict and rep.errors) else 0
    if a.check and rc == 0:
        rc = cmd_check(argparse.Namespace(**{**vars(a), "dry_run": False}))
    return rc


def cmd_check(a) -> int:
    res = run_check(Path(a.dst), eld=a.eld, build_dir=Path(a.build_dir) if a.build_dir else None,
                    jobs=a.jobs or default_jobs(), dry_run=a.dry_run, extra_cmake=a.cmake_arg or [], probe=not a.no_probe,
                    clean=not a.no_clean, build_args=a.build_arg or [],
                    timeout=getattr(a, "timeout", None), pre_build=getattr(a, "pre_build", None) or [],
                    allow_unresolved_data=getattr(a, "allow_unresolved_data", False),
                    allow_ignored_version_scripts=getattr(a, "allow_ignored_version_scripts", False),
                    compare=getattr(a, "compare", None),
                    reference_dir=Path(a.reference_dir) if getattr(a, "reference_dir", None) else None,
                    log=_log(a))
    if a.dry_run:
        for c in res.commands:
            print(" ".join(c))
        return 0
    if res.eld is None:
        print("error: " + "; ".join(res.problems), file=sys.stderr)
        return 2    # usage/input error, like `audit` and `surface`
    if res.returncode and res.stderr_tail:
        print("--- stderr tail ---\n" + res.stderr_tail, file=sys.stderr)
    if getattr(a, "json", False):
        print(json.dumps({k: v for k, v in res.as_dict().items() if k not in ("stdout_tail",)}, indent=2))
        print("CHECK " + ("PASSED" if res.ok else "FAILED"))
        return 0 if res.ok else 1
    from .checker import CHECK_MARKDOWN, CHECK_REPORT
    from .report import check_text, use_color
    bd = Path(res.build_dir) if res.build_dir else None
    files = [str(bd / CHECK_MARKDOWN), str(bd / CHECK_REPORT)] if bd else []
    files += [f"{res.build_log} (build log)" if res.build_log else ""]
    if res.reference:
        files.append(f"{res.reference.get('build_log')} (original build log)")
    print(check_text(res.as_dict(), Path(a.dst).resolve(), color=use_color(sys.stdout),
                     verbose=bool(getattr(a, "verbose", 0)), files=files))
    return 0 if res.ok else 1


def cmd_audit(a) -> int:
    from .checker import find_eld
    eld = find_eld(a.eld)
    if eld is None:
        print("eldconv: ld.eld not found (pass --eld or put it on PATH)", file=sys.stderr)
        return 2
    res = audit(eld, probe_acceptance=not a.no_probe_acceptance, log=_log(a))
    print(json.dumps(res.as_dict(), indent=2))
    bad = len(res.by_kind("missing"))
    print(f"AUDIT: {bad} rule(s) this eld would reject, "
          f"{len(res.by_kind('needless'))} needless drop(s), "
          f"{len(res.by_kind('confirmed'))} tentative rule(s) confirmed, "
          f"{len(res.by_kind('unknown'))} option(s) not in the table", file=sys.stderr)
    return 1 if bad else 0


def cmd_surface(a) -> int:
    from .checker import find_eld
    from .surface import build
    eld = find_eld(a.eld)
    if eld is None:
        print("eldconv: ld.eld not found (pass --eld or put it on PATH)", file=sys.stderr)
        return 2
    log = _log(a)
    log.step(f"probing {eld} with every option bfd/gold/lld/mold know")

    def progress(done: int, total: int) -> None:
        if log.enabled and (done % 100 == 0 or done == total):
            log.info(f"{done}/{total}")
    s = build(eld, jobs=a.jobs, progress=progress)
    text = json.dumps(s.as_dict(), indent=0, separators=(",", ":")) + "\n"
    if a.output and a.output != "-":
        Path(a.output).write_text(text)
    else:
        sys.stdout.write(text)
    acc = sum(1 for v in s.options.values() if v)
    print(f"SURFACE: {s.eld_version or 'unknown eld'}; targets {', '.join(s.targets)}; "
          f"{acc}/{len(s.options)} options and {sum(1 for v in s.z.values() if v)}/{len(s.z)} "
          f"-z keywords accepted", file=sys.stderr)
    return 0


def cmd_compare(a) -> int:
    from .elf import open_elf
    from .elfcheck import compare, validate
    from .layout import layout
    from .report import compare_markdown, compare_text, use_color
    with open_elf(a.reference) as ref, open_elf(a.output) as out:
        for p, e in ((a.reference, ref), (a.output, out)):
            if e is None:
                print(f"eldconv: {p} is not an ELF file", file=sys.stderr)
                return 2
        found = [dict(f.as_dict(), check="validate", what="image: " + f.what) for f in validate(out)] \
            + [dict(f.as_dict(), check="compare") for f in compare(ref, out, scripted=a.script)]
        lay = {"reference": layout(ref), "eld": layout(out)}
    if a.report:
        Path(a.report).write_text(compare_markdown(a.reference, a.output, found, lay))
    if a.json:
        print(json.dumps({"reference": a.reference, "output": a.output, "findings": found,
                          "layout": lay}, indent=2))
    else:
        print(compare_text(a.reference, a.output, found, lay, color=use_color(sys.stdout),
                           verbose=bool(a.verbose)))
    if a.report:
        print(f"report: {a.report}", file=sys.stderr)
    return 1 if any(f["level"] == "error" for f in found) else 0


def cmd_flags(a) -> int:
    log = _log(a)
    _apply_surface(a)
    _apply_target(a)
    fn = convert_raw_string if a.raw else convert_driver_string
    text = " ".join(a.flags)
    log.step(f"converting {len(text)} char(s) of {'raw ld argv' if a.raw else 'driver flags'} "
             f"from {a.source_linker}")
    out, diags = fn(text, a.source_linker)
    _print_diags(diags)
    print(out)
    return 0


TARGET_HELP = ("the architecture the build links for (x86_64, aarch64, arm, riscv64, riscv32, i386, "
               "hexagon, or a triple): an option eld accepts only for other targets is dropped "
               "(default: keep an option eld accepts for any target; a link's own -m wins)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eldconv", description="Convert bfd/lld/gold/mold build setups to eld.")
    p.add_argument("--version", action="version", version=__version__)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0,
                        help="explain each step on stderr; -vv also lists every file "
                             "scanned and every flag decision")
    sub = p.add_subparsers(dest="cmd", required=True)

    # what `check` builds with; `convert --check` takes the same options
    chk = argparse.ArgumentParser(add_help=False)
    g = chk.add_argument_group("build and verify (check, convert --check)")
    g.add_argument("--build-dir", help="where to build (default: DST/build-eld, or DST for "
                                       "in-tree builds); relative to DST")
    g.add_argument("--jobs", "-j", type=int, default=None,
                   help=f"parallel build jobs (default: the number of CPUs, {default_jobs()} here)")
    g.add_argument("--no-probe", action="store_true", help="skip `ld.eld --help` option validation")
    g.add_argument("--build-arg", action="append", metavar="VAR=VAL",
                   help="extra argument for the build step, e.g. ARCH=arm64 "
                        "(repeatable); a cross build needs the same variables "
                        "you would pass by hand")
    g.add_argument("--pre-build", action="append", metavar="CMD",
                   help="shell command run in DST before the build (repeatable), e.g. "
                        "'./configure' for a project whose own configure script generates "
                        "its Makefile; it sees the same shim environment as the build")
    g.add_argument("--allow-unresolved-data", action="store_true",
                   help="report undefined data symbols that got no relocation (a binary that "
                        "links but crashes, e.g. on x86_64 with eld) as a warning, not a failure")
    g.add_argument("--allow-ignored-version-scripts", action="store_true",
                   help="report version scripts eld skipped (an eld built without "
                        "ELD_ENABLE_SYMBOL_VERSIONING) as a warning, not a failure")
    g.add_argument("--no-clean", action="store_true",
                   help="build incrementally (an up-to-date tree then verifies nothing)")
    g.add_argument("--timeout", type=int, default=None, help="per-command timeout in seconds")
    cg = g.add_mutually_exclusive_group()
    cg.add_argument("--compare", dest="compare", action="store_const", const=True, default=None,
                    help="require the comparison with the original build: fail the check if the "
                         "original tree cannot be built (by default it is compared when it can be)")
    cg.add_argument("--no-compare", dest="compare", action="store_const", const=False,
                    help="do not build the original tree with its own linker for comparison")
    g.add_argument("--reference-dir", metavar="DIR",
                   help="where to build the original tree for the comparison (default: DST.reference)")
    g.add_argument("--json", action="store_true",
                   help="print the result as JSON instead of the report (both are saved in the build dir)")

    d = sub.add_parser("detect", parents=[common], help="detect build system / toolchain / linker")
    d.add_argument("src")
    d.add_argument("--source-linker", choices=LINKERS)
    d.add_argument("--build-system", choices=SYSTEMS, help="override the detected build system")
    d.set_defaults(fn=cmd_detect)

    c = sub.add_parser("convert", parents=[common, chk], help="generate an eld-linked copy of SRC in DST")
    c.add_argument("src")
    c.add_argument("dst")
    c.add_argument("--eld", default="ld.eld", help="path to ld.eld (default: ld.eld on PATH)")
    c.add_argument("--source-linker", choices=LINKERS, help="override detected linker")
    c.add_argument("--build-system", choices=SYSTEMS,
                   help="override the detected build system (a tree may carry several)")
    c.add_argument("--toolchain-file", help="original CMake toolchain file to chain-include")
    c.add_argument("--cmake-arg", action="append", help="extra -D args for configure")
    c.add_argument("--force", action="store_true", help="replace DST if it exists")
    c.add_argument("--check", action="store_true", help="run the checker after converting")
    c.add_argument("--quiet", "-q", action="store_true")
    c.add_argument("--strict", action="store_true",
                   help="exit 1 if the conversion reported any error (semantics lost)")
    c.add_argument("--eld-surface", metavar="FILE|auto|bundled|none",
                   help="what eld accepts: the bundled snapshot (default), auto (probe --eld "
                        "once, then cache), a file from `eldconv surface`, or none to trust "
                        "the rule table alone")
    c.add_argument("--target", metavar="ARCH", help=TARGET_HELP)
    c.set_defaults(fn=cmd_convert)

    k = sub.add_parser("check", parents=[common, chk], help="build DST with eld and verify")
    k.add_argument("dst")
    k.add_argument("--eld")
    k.add_argument("--cmake-arg", action="append")
    k.add_argument("--dry-run", action="store_true", help="print the commands only")
    k.set_defaults(fn=cmd_check)

    au = sub.add_parser("audit", parents=[common],
                        help="compare the mapping table with a real ld.eld")
    au.add_argument("--eld", help="path to ld.eld (default: ld.eld on PATH)")
    au.add_argument("--no-probe-acceptance", action="store_true",
                    help="trust `--help` alone; do not offer options to the linker")
    au.set_defaults(fn=cmd_audit)

    sf = sub.add_parser("surface", parents=[common],
                        help="probe a ld.eld and write the option surface it accepts")
    sf.add_argument("--eld", help="path to ld.eld (default: ld.eld on PATH)")
    sf.add_argument("-o", "--output", help="output file (default: stdout)")
    sf.add_argument("--jobs", "-j", type=int, default=8)
    sf.set_defaults(fn=cmd_surface)

    cp = sub.add_parser("compare", parents=[common],
                        help="check an ELF file and compare it with the same file from another linker")
    cp.add_argument("reference", help="the file as the original linker made it")
    cp.add_argument("output", help="the file as eld made it")
    cp.add_argument("--script", action="store_true",
                    help="the image is placed by a linker script: hold its layout (base address, "
                         "section order, load addresses, section-edge symbols) to the reference's")
    cp.add_argument("--report", metavar="FILE", help="also write the comparison as Markdown to FILE")
    cp.add_argument("--json", action="store_true", help="print JSON instead of the report")
    cp.set_defaults(fn=cmd_compare)

    f = sub.add_parser("flags", parents=[common], help="convert a flag string")
    f.add_argument("flags", nargs="+", help="flag string (leading dashes are fine)")
    f.add_argument("--source-linker", choices=LINKERS, default="bfd")
    f.add_argument("--raw", action="store_true", help="treat input as a raw ld argv, not driver flags")
    f.add_argument("--eld-surface", metavar="FILE|auto|bundled|none")
    f.add_argument("--eld", help="the ld.eld --eld-surface auto probes (default: ld.eld on PATH)")
    f.add_argument("--target", metavar="ARCH", help=TARGET_HELP)
    f.set_defaults(fn=cmd_flags)
    return p


def _protect_flag_operands(argv: List[str]) -> List[str]:
    """`eldconv flags -Wl,--gc-sections` must not be parsed as eldconv's own options."""
    if not argv or argv[0] != "flags" or "--" in argv:
        return argv
    opts: List[str] = []
    rest: List[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if (a in ("--raw", "-h", "--help", "-v", "--verbose", "-vv")
                or a.startswith(("--source-linker=", "--eld-surface=", "--eld=", "--target="))):
            opts.append(a)
            i += 1
        elif a in ("--source-linker", "--eld-surface", "--eld", "--target"):
            opts.extend(argv[i:i + 2])
            i += 2
        else:
            rest.append(a)
            i += 1
    return ["flags", *opts, "--", *rest] if rest else argv


def main(argv=None) -> int:
    argv = _protect_flag_operands(list(sys.argv[1:] if argv is None else argv))
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        print("eldconv: interrupted", file=sys.stderr)
        return 130
    except (FileExistsError, ValueError, RuntimeError) as e:
        print(f"eldconv: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"eldconv: {e.strerror or e}{': ' + str(e.filename) if e.filename else ''}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
