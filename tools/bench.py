#!/usr/bin/env python3
"""Benchmark eldconv and the linkers it targets.

  python tools/bench.py eldconv --src DIR [--src DIR ...] --eld PATH [-n 3]
  python tools/bench.py links --log BUILD/eld-link.log --linker eld=PATH --linker bfd=ld.bfd ... [-n 5]
  python tools/bench.py shim --dst CONVERTED_TREE [-n 200]

`eldconv` times detect and convert on each source tree, flag conversion throughput and an
`eldconv surface` probe. `links` replays the real link commands an `eldconv check` recorded
(the shim log) with each linker and reports wall time, CPU time and peak RSS. `shim` measures
what the shim adds to one link. Every number is the median of -n runs; output is JSON on stdout
and a table on stderr. Linux only (uses wait4 for per-process rusage).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: Sequence[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> Dict[str, float]:
    """Wall time, user+sys CPU time and peak RSS of one process tree."""
    t0 = time.perf_counter()
    p = subprocess.Popen(list(cmd), cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.perf_counter() - t0
    err = p.stderr.read().decode(errors="replace") if p.stderr else ""
    p.stderr.close() if p.stderr else None
    rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8
    return {"wall": wall, "cpu": ru.ru_utime + ru.ru_stime, "rss_mb": ru.ru_maxrss / 1024, "rc": rc,
            "err": err[-400:]}


def median_of(n: int, fn) -> Dict[str, float]:
    rows = [fn() for _ in range(n)]
    out = {k: statistics.median(r[k] for r in rows) for k in ("wall", "cpu", "rss_mb")}
    out["rc"] = max(r["rc"] for r in rows)
    out["runs"] = n
    if out["rc"]:
        out["err"] = rows[-1]["err"]
    return out


def eldconv_cmd(*args: str) -> List[str]:
    return [sys.executable, "-m", "eldconv", *args]


def tree_stats(src: Path) -> Dict[str, int]:
    files = size = 0
    for dp, dn, fn in os.walk(src):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            files += 1
            try:
                size += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return {"files": files, "mb": round(size / 1e6, 1)}


def bench_eldconv(a) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    res: dict = {"trees": {}}
    work = Path(tempfile.mkdtemp(prefix="eldconv-bench-"))
    try:
        for src in a.src:
            src = Path(src).resolve()
            dst = work / src.name

            def conv():
                shutil.rmtree(dst, ignore_errors=True)
                return run(eldconv_cmd("convert", str(src), str(dst), "--eld", a.eld, "-q"), env=env)
            row = {**tree_stats(src),
                   "detect": median_of(a.n, lambda: run(eldconv_cmd("detect", str(src)), env=env)),
                   "convert": median_of(a.n, conv)}
            rep = json.loads((dst / "eldconv-report.json").read_text())
            row["rewritten"] = len(rep.get("files_rewritten") or [])
            row["diagnostics"] = len(rep.get("diagnostics") or [])
            res["trees"][src.name] = row
        sys.path.insert(0, str(ROOT))
        from eldconv.flags import parse_wl
        from eldconv.mapping import convert_args
        spec = ("-Wl,--gc-sections,-z,now,-z,relro,--icf=all,-O1,--as-needed,-Map,out.map "
                "-Wl,--version-script=v.map,--pic-veneer,--build-id=sha1,-rpath,$ORIGIN")
        k = 20000
        t0 = time.perf_counter()
        for _ in range(k):
            convert_args(parse_wl(spec), "lld")
        dt = time.perf_counter() - t0
        res["flags"] = {"strings": k, "tokens_each": len(parse_wl(spec)), "per_string_us": dt / k * 1e6}
        if a.surface:
            out = work / "s.json"
            res["surface"] = median_of(1, lambda: run(eldconv_cmd("surface", "--eld", a.eld, "-o", str(out),
                                                                  "-j", str(a.jobs)), env=env))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return res


def _link_records(log: Path) -> List[dict]:
    sys.path.insert(0, str(ROOT))
    from eldconv.checker import parse_link_log
    recs = []
    for inv in parse_link_log(log):
        if inv.is_probe() or inv.is_trycompile():
            continue
        out = inv.output_path()
        if out is None or not out.exists():
            continue
        recs.append({"cwd": inv.cwd, "args": inv.args, "output": str(out)})
    return recs


def bench_links(a) -> dict:
    linkers = dict(x.split("=", 1) for x in a.linker)
    recs = _link_records(Path(a.log))
    if a.only:
        recs = [r for r in recs if any(s in r["output"] for s in a.only)]
    res: dict = {}
    for r in recs:
        name = os.path.relpath(r["output"], r["cwd"])
        row: dict = {}
        for lname, lpath in linkers.items():
            tmp = tempfile.mkdtemp(prefix="eldconv-link-")
            out = os.path.join(tmp, "out")
            args = list(r["args"])
            i = args.index("-o") if "-o" in args else -1
            if i >= 0:
                args[i + 1] = out
            else:
                args = [a2 if not a2.startswith("-o") else "-o" + out for a2 in args]
            extra = a.extra.get(lname, []) if isinstance(a.extra, dict) else []
            m = median_of(a.n, lambda: run([lpath, *extra, *args], cwd=r["cwd"]))
            m["out_kb"] = round(os.path.getsize(out) / 1024) if os.path.exists(out) else None
            row[lname] = m
            shutil.rmtree(tmp, ignore_errors=True)
        res[name] = row
    return res


def bench_shim(a) -> dict:
    """One trivial link straight through ld.eld and through DST/eld-bin/ld."""
    dst = Path(a.dst).resolve()
    shim = dst / "eld-bin" / "ld"
    eld = Path(shim.read_text().split("ELDCONV_ELD:-", 1)[1].split("}", 1)[0]) \
        if "ELDCONV_ELD:-" in shim.read_text() else None
    eld = Path(a.eld) if a.eld else eld
    work = Path(tempfile.mkdtemp(prefix="eldconv-shim-"))
    try:
        (work / "a.c").write_text("int main(void){return 0;}\n")
        subprocess.run(["cc", "-c", str(work / "a.c"), "-o", str(work / "a.o")], check=True)
        args = ["-o", str(work / "a.out"), "-e", "main", str(work / "a.o")]
        log = work / "log"
        env = dict(os.environ, ELDCONV_LINK_LOG=str(log))
        direct = median_of(a.n, lambda: run([str(eld), *args]))
        via = median_of(a.n, lambda: run([str(shim), *args], env=env))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"eld": str(eld), "direct": direct, "via_shim": via,
            "overhead_ms": (via["wall"] - direct["wall"]) * 1000}


def table(res: dict, mode: str) -> str:
    lines = []
    if mode == "eldconv":
        lines.append(f"{'tree':<12}{'files':>8}{'MB':>8}{'detect s':>10}{'convert s':>11}{'RSS MB':>8}")
        for n, r in res["trees"].items():
            lines.append(f"{n:<12}{r['files']:>8}{r['mb']:>8}{r['detect']['wall']:>10.2f}"
                         f"{r['convert']['wall']:>11.2f}{r['convert']['rss_mb']:>8.0f}")
        f = res["flags"]
        lines.append(f"flags: {f['per_string_us']:.0f} us per {f['tokens_each']}-option string")
        if "surface" in res:
            lines.append(f"surface probe: {res['surface']['wall']:.0f} s wall, {res['surface']['cpu']:.0f} s CPU")
    elif mode == "links":
        for n, row in res.items():
            lines.append(n)
            for ln, m in row.items():
                ok = "" if not m["rc"] else f"  FAILED rc={m['rc']}"
                lines.append(f"  {ln:<6} {m['wall']*1000:9.0f} ms wall {m['cpu']*1000:9.0f} ms CPU "
                             f"{m['rss_mb']:7.0f} MB{ok}")
    else:
        lines.append(f"direct {res['direct']['wall']*1000:.1f} ms, via shim {res['via_shim']['wall']*1000:.1f} ms, "
                     f"overhead {res['overhead_ms']:.1f} ms per link")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    e = sub.add_parser("eldconv")
    e.add_argument("--src", action="append", required=True)
    e.add_argument("--eld", required=True)
    e.add_argument("--surface", action="store_true", help="also time `eldconv surface` (minutes)")
    e.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    e.add_argument("-n", type=int, default=3)
    li = sub.add_parser("links")
    li.add_argument("--log", required=True, help="eld-link.log written by `eldconv check`")
    li.add_argument("--linker", action="append", required=True, metavar="NAME=PATH")
    li.add_argument("--only", action="append", help="only outputs whose path contains this")
    li.add_argument("-n", type=int, default=5)
    s = sub.add_parser("shim")
    s.add_argument("--dst", required=True)
    s.add_argument("--eld")
    s.add_argument("-n", type=int, default=200)
    a = p.parse_args(argv)
    a.extra = {}
    res = {"eldconv": bench_eldconv, "links": bench_links, "shim": bench_shim}[a.mode](a)
    res_meta = {"cpus": os.cpu_count(), "python": sys.version.split()[0], "results": res}
    print(json.dumps(res_meta, indent=1, default=str))
    print(table(res, a.mode), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
