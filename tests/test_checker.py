import json
import struct
import subprocess
from pathlib import Path

import pytest

from eldconv.checker import (CHECK_REPORT, clean_commands, LOG_NAME, LinkInvocation, build_commands, elf_type, is_elf,
                             parse_help, parse_link_log, probe_eld, run_check, unknown_flags)
from eldconv.cli import main
from eldconv.generator import convert_project

FIX = Path(__file__).parent / "fixtures"

HELP = """OVERVIEW: eld linker
USAGE: ld.eld [options] <inputs>

OPTIONS:
  --allow-multiple-definition   Allow multiple definitions
  --as-needed                   Only set DT_NEEDED for shared libraries if used
  --gc-sections                 Enable garbage collection of unused sections
  -Map=<file>, --Map=<file>     Print a link map to the specified file
  -e <entry>, --entry=<entry>   Name of entry point symbol
  --no-threads                  Do not run the linker multi-threaded
  --reproduce=<file>            Write tar file containing inputs and command to reproduce link
  --threads                     Run the linker multi-threaded
  -z <option>                   Linker option extensions
  --start-lib                   Start a grouping of objects that should be treated as if they were together in an archive
"""


def fake_elf(path: Path, e_type=2):
    hdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8 + struct.pack("<HH", e_type, 0x3E) + b"\x00" * 44
    path.write_bytes(hdr + b"\x00" * 64)


# ---------------------------------------------------------------- pieces

def test_parse_help():
    known = parse_help(HELP)
    assert {"--gc-sections", "-Map", "--Map", "-e", "--entry", "--threads", "-z", "--reproduce", "--start-lib"} <= known
    assert "OPTIONS:" not in known


def test_invocation_output_forms():
    assert LinkInvocation("/b", ["-o", "a.out", "x.o"]).output == "a.out"
    assert LinkInvocation("/b", ["-oa.out", "x.o"]).output == "a.out"
    assert LinkInvocation("/b", ["--output=a.out"]).output == "a.out"
    assert LinkInvocation("/b", ["x.o"]).output is None
    assert LinkInvocation("/b", ["-o", "a.out"]).output_path() == Path("/b/a.out")
    assert LinkInvocation("/b", ["-o", "/abs/a.out"]).output_path() == Path("/abs/a.out")


def test_invocation_option_names():
    inv = LinkInvocation("/", ["--gc-sections", "--icf=all", "-lm", "-L/x", "-znow", "-O2", "-Map", "m", "foo.o", "-"])
    assert inv.option_names() == ["--gc-sections", "--icf", "-l", "-L", "-z", "-O", "-Map"]


def test_unknown_flags():
    known = parse_help(HELP)
    inv = LinkInvocation("/", ["--gc-sections", "--icf=all", "-lm", "-Map", "m", "--fork"])
    assert unknown_flags(inv, known) == ["--fork", "--icf"]
    assert unknown_flags(inv, set()) == []  # no probe -> no judgement


def test_parse_link_log(tmp_path):
    log = tmp_path / LOG_NAME
    log.write_text("/build\x1f-o\x1fapp\x1fmain.o\x1f\n/build/sub\x1f-o\x1flib.so\x1f-shared\x1f\n\n")
    invs = parse_link_log(log)
    assert len(invs) == 2 and invs[0].args == ["-o", "app", "main.o"] and invs[1].cwd == "/build/sub"
    assert parse_link_log(tmp_path / "missing") == []


def test_is_elf(tmp_path):
    p = tmp_path / "a.out"
    fake_elf(p)
    assert is_elf(p) and elf_type(p) == "EXEC"
    fake_elf(p, 3)
    assert elf_type(p) == "DYN"
    (tmp_path / "t.txt").write_text("hello")
    assert not is_elf(tmp_path / "t.txt")
    assert not is_elf(tmp_path / "none")


def test_probe_eld_with_runner():
    def runner(cmd, **kw):
        if cmd[1] == "--help":
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        return subprocess.CompletedProcess(cmd, 0, "eld 1.2.3\n", "")
    known, ver = probe_eld("/fake/ld.eld", runner)
    assert "--gc-sections" in known and ver == "eld 1.2.3"


def test_probe_eld_failure_is_soft():
    def runner(cmd, **kw):
        raise OSError("nope")
    known, ver = probe_eld("/fake/ld.eld", runner)
    assert known == set() and ver is None


def test_build_commands(tmp_path):
    dst = tmp_path / "c"
    convert_project(FIX / "cmake_lld", dst, eld="/e/ld.eld")
    cmds = build_commands(dst, dst / "b", "/e/ld.eld", 2, ["-DFOO=1"])
    assert cmds[0][0] == "cmake" and "-DFOO=1" in cmds[0]
    assert not any(c.startswith("-DELD_LINKER") for c in cmds[0])  # env ELDCONV_ELD is the knob
    assert cmds[1][:2] == ["cmake", "--build"]
    dst2 = tmp_path / "m"
    convert_project(FIX / "make_gold", dst2, eld="/e/ld.eld")
    assert build_commands(dst2, dst2 / "b", "/e/ld.eld", 3, [])[0][:2] == ["make", "-C"]
    with pytest.raises(ValueError):
        build_commands(tmp_path, tmp_path / "b", "/e/ld.eld", 1, [])


# ---------------------------------------------------------------- run_check with a fake build

def make_runner(help_text=HELP, build_rc=0, link_outputs=None, write_log=True):
    """Runner that simulates configure/build: writes the shim log and output files."""
    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, help_text if cmd[1] == "--help" else "eld test", "")
        env = kw.get("env", {})
        if cmd[0] in ("make", "cmake") and (cmd[0] == "make" or cmd[1] == "--build"):
            log = Path(env["ELDCONV_LINK_LOG"])
            log.parent.mkdir(parents=True, exist_ok=True)
            if write_log:
                with open(log, "a") as fh:
                    for name, args in (link_outputs or {}).items():
                        out = log.parent / name
                        fake_elf(out) if not name.endswith(".bad") else out.write_text("not elf")
                        fh.write("\x1f".join([str(log.parent), "-o", name, *args]) + "\x1f\n")
            return subprocess.CompletedProcess(cmd, build_rc, "built", "" if build_rc == 0 else "error: boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return runner


@pytest.fixture
def fake_eld(tmp_path):
    p = tmp_path / "ld.eld"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return str(p)


def test_run_check_pass(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(link_outputs={"app": ["--gc-sections", "main.o"]}))
    assert res.ok, res.problems
    assert res.invocations == 1 and res.outputs[0]["elf"] and res.outputs[0]["elf_type"] == "EXEC"
    assert res.eld_version == "eld test"
    rep = json.loads((Path(res.build_dir) / CHECK_REPORT).read_text())
    assert rep["ok"] is True


def test_relative_build_dir_is_anchored_under_dst(tmp_path, fake_eld, monkeypatch):
    """A relative --build-dir must resolve against the project (dst), not
    against whatever directory the eldconv *process* happens to be run from.

    Found via real-eld validation: a GN project checked with `--build-dir
    out/eld` (a relative path, exactly as eldconv's own generated
    build-eld.sh script uses it) silently mis-rooted log_path/env/mkdir
    against the CWD instead of dst. `gn`/`ninja` still ran correctly (they
    were invoked with cwd=dst), and eld genuinely linked everything -- but
    the shim's ELDCONV_LINK_LOG pointed at a path nothing ever created, so a
    build that fully succeeded through eld was reported as CHECK FAILED
    ("no link step went through eld").
    """
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    other_cwd = tmp_path / "somewhere-else"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    res = run_check(dst, build_dir="rel-build",
                     runner=make_runner(link_outputs={"app": ["--gc-sections", "main.o"]}))
    assert res.build_dir == str(dst / "rel-build")
    assert res.ok, res.problems
    assert (dst / "rel-build" / LOG_NAME).exists()
    assert not (other_cwd / "rel-build").exists()


def test_run_check_build_failure(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(build_rc=2, write_log=False))
    assert not res.ok and any("build failed" in p for p in res.problems) and "boom" in res.stderr_tail


def test_run_check_detects_eld_not_used(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(write_log=False))
    assert not res.ok and any("no link step went through eld" in p for p in res.problems)


def test_run_check_flags_unknown_to_eld(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(link_outputs={"app": ["--gc-sections", "--fork", "main.o"]}))
    assert res.ok and res.unknown_flags == ["--fork"] and any("--fork" in w for w in res.warnings)
    res = run_check(dst, runner=make_runner(build_rc=1, link_outputs={"app": ["--fork", "main.o"]}))
    assert not res.ok and any("--fork" in p for p in res.problems)


def test_run_check_bad_elf(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(link_outputs={"app.bad": ["main.o"]}))
    assert not res.ok and any("not a valid ELF" in p for p in res.problems)


def test_run_check_tentative_confirmation(tmp_path, fake_eld):
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text("LDFLAGS = -Wl,--start-lib -Wl,--push-state\napp:\n\t$(CC) $(LDFLAGS) -o app x.c\n")
    dst = tmp_path / "c"
    convert_project(src, dst, eld=fake_eld)
    # build succeeded and both flags were used -> both confirmed
    res = run_check(dst, runner=make_runner(link_outputs={"app": ["--start-lib", "--push-state", "x.o"]}))
    assert res.ok and res.tentative_confirmed == {"--start-lib": True, "--push-state": True}
    # build failed: --start-lib is in HELP (confirmed), --push-state is not
    res = run_check(dst, runner=make_runner(build_rc=1, link_outputs={"app": ["--start-lib", "--push-state", "x.o"]}))
    assert res.tentative_confirmed == {"--start-lib": True, "--push-state": False}
    assert any("--push-state" in w for w in res.warnings)


def test_run_check_dry_run_and_missing_eld(tmp_path):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld="/definitely/not/here/ld.eld")
    res = run_check(dst, dry_run=True)
    assert not res.ok and "not found" in res.problems[0]
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    res = run_check(dst, eld=str(fake), dry_run=True)
    assert res.ok and res.commands[0][0] == "make"


# ---------------------------------------------------------------- cli

def test_cli_flags(capsys):
    assert main(["flags", "--source-linker", "lld", "-Wl,--icf=all -Wl,--gc-sections"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "-Wl,--gc-sections" and "identical-code-folding" in out.err


def test_cli_detect_and_convert(tmp_path, capsys):
    assert main(["detect", str(FIX / "make_mold")]) == 0
    assert json.loads(capsys.readouterr().out)["linker"] == "mold"
    dst = tmp_path / "out"
    assert main(["convert", str(FIX / "make_mold"), str(dst), "--eld", "/x/ld.eld", "-q"]) == 0
    assert "converted" in capsys.readouterr().out and (dst / "eld.mk").exists()
    assert main(["convert", str(FIX / "make_mold"), str(dst)]) == 2  # exists


def test_cli_check_dry_run(tmp_path, capsys):
    dst = tmp_path / "out"
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    main(["convert", str(FIX / "cmake_lld"), str(dst), "--eld", str(fake), "-q"])
    capsys.readouterr()
    assert main(["check", str(dst), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "cmake -S" in out and "cmake --build" in out


# ---------------------------------------------------------------- classification regressions

def test_probe_vs_link_classification():
    """Driver/CMake capability queries went through the shim and were counted as links."""
    probe = [LinkInvocation("/b", ["-v"]), LinkInvocation("/b", ["--help"]),
             LinkInvocation("/b", ["--push-state", "--pop-state"]),
             LinkInvocation("/b", ["-m", "elf", "--push-state", "--pop-state"])]  # elf is -m's value
    for inv in probe:
        assert inv.is_probe(), inv.args
    # a real link may carry -v, and is identified by having an output or inputs
    links = [LinkInvocation("/b", ["-v", "-o", "cmTC_1", "a.o"]),
             LinkInvocation("/b", ["-o", "app", "main.o"]),
             LinkInvocation("/b", ["-r", "a.o", "b.o"])]
    for inv in links:
        assert not inv.is_probe(), inv.args
    assert LinkInvocation("/x/CMakeFiles/CMakeScratch/TryCompile-ab/", ["-o", "t"]).is_trycompile()
    assert not LinkInvocation("/x/build", ["-o", "t"]).is_trycompile()


def test_run_check_ignores_probes_and_trycompiles(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)

    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        env = kw.get("env", {})
        log = Path(env["ELDCONV_LINK_LOG"])
        log.parent.mkdir(parents=True, exist_ok=True)
        scratch = log.parent / "CMakeFiles" / "CMakeScratch" / "TryCompile-x"
        fake_elf(log.parent / "app")
        with open(log, "a") as fh:
            fh.write(f"{log.parent}\x1f-v\x1f\n")                       # probe
            fh.write(f"{log.parent}\x1f-m\x1felf\x1f--push-state\x1f\n")  # probe
            fh.write(f"{scratch}\x1f-o\x1fcmTC_1\x1fa.o\x1f\n")          # try_compile, deleted
            fh.write(f"{log.parent}\x1f-o\x1fapp\x1fmain.o\x1f\n")       # real link
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = run_check(dst, runner=runner)
    assert res.ok, res.problems
    assert (res.invocations, res.probes, res.trycompiles) == (1, 2, 1)


def test_run_check_reports_up_to_date_build(tmp_path, fake_eld):
    """A build that relinks nothing must not silently pass as verified."""
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)

    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        env = kw.get("env", {})
        log = Path(env["ELDCONV_LINK_LOG"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(f"{log.parent}\x1f-v\x1f\n")  # only a probe: nothing relinked
        return subprocess.CompletedProcess(cmd, 0, "make: nothing to be done", "")

    res = run_check(dst, runner=runner)
    assert not res.ok and any("up to date" in p for p in res.problems)


def test_run_check_cleans_before_building(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        return make_runner(link_outputs={"app": ["main.o"]})(cmd, **kw)

    run_check(dst, runner=runner)
    assert ["make", "-C", str(dst), "clean"] in seen
    seen.clear()
    run_check(dst, runner=runner, clean=False)
    assert ["make", "-C", str(dst), "clean"] not in seen


def test_run_check_removes_stale_cmake_build_dir(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "cmake_lld", dst, eld=fake_eld)
    stale = dst / "build-eld"
    stale.mkdir()
    (stale / "CMakeCache.txt").write_text("stale")
    run_check(dst, runner=make_runner(link_outputs={"app": ["main.o"]}))
    assert not (stale / "CMakeCache.txt").exists()


def test_cli_flags_accepts_leading_dash_operands(capsys):
    assert main(["flags", "-Wl,--fork", "--source-linker", "mold"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert main(["flags", "--source-linker", "lld", "-Wl,--icf=all", "-Wl,--gc-sections"]) == 0
    assert capsys.readouterr().out.strip() == "-Wl,--gc-sections"
    assert main(["flags", "--raw", "--source-linker", "gold", "--", "--icf=all", "-o", "a.out"]) == 0
    assert capsys.readouterr().out.strip() == "-o a.out"


# ---------------------------------------------------------------- eld ignores unknown flags

def test_ignored_options_parsing():
    from eldconv.checker import ignored_options
    # the exact wording ld.eld 24.0 uses
    out = ("Warning: Unrecognized option `--icf=all'\n"
           "Warning: Unrecognized option `--fork'\n")
    assert ignored_options(out) == ["--fork", "--icf=all"]
    # other linkers' phrasings
    assert ignored_options("ld: unknown option: --foo") == ["--foo"]
    assert ignored_options("lld: error: unknown argument '--bar'") == ["--bar"]
    assert ignored_options("nothing to see") == []


def test_silently_ignored_flags_fail_the_check(tmp_path, fake_eld):
    """Real eld warns and exits 0, so a successful build can still be wrong."""
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)

    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        env = kw.get("env", {})
        log = Path(env["ELDCONV_LINK_LOG"])
        log.parent.mkdir(parents=True, exist_ok=True)
        fake_elf(log.parent / "app")
        with open(log, "a") as fh:
            fh.write(f"{log.parent}\x1f--icf=all\x1f-o\x1fapp\x1fmain.o\x1f\n")
        return subprocess.CompletedProcess(
            cmd, 0, "", "Warning: Unrecognized option `--icf=all'\n")

    res = run_check(dst, runner=runner)
    assert not res.ok
    assert res.returncode == 0                      # the build itself succeeded
    assert res.ignored_flags == ["--icf=all"]
    assert any("silently ignored" in p for p in res.problems)


def test_clean_build_reports_no_ignored_flags(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    res = run_check(dst, runner=make_runner(link_outputs={"app": ["--gc-sections", "main.o"]}))
    assert res.ok and res.ignored_flags == []


def test_build_args_are_passed_to_the_build_step(tmp_path, fake_eld):
    """A cross build needs the same variables the user would pass by hand;
    without them the build silently targets the host."""
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    cmds = build_commands(dst, dst / "b", fake_eld, 2, [],
                          ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-"])
    assert cmds[-1][-2:] == ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-"]
    assert clean_commands(dst, dst / "b", ["ARCH=arm64"])[-1][-1] == "ARCH=arm64"


def test_clean_commands_drop_target_names_but_keep_var_assignments(tmp_path, fake_eld):
    """Found via real-eld kernel validation: `--build-arg vmlinux` (a target
    name, not a VAR=VAL assignment) must reach the build step but NOT the
    `make clean` step. Appending it to clean too turns "make clean" into
    "make clean vmlinux", which builds and links vmlinux *during the clean
    phase* -- the real eld link gets logged there, and that log is then
    deleted (as part of resetting it for the "real" build) before the
    nominal build step runs and finds nothing left to do. The upshot was a
    build that fully succeeded through eld being reported as CHECK FAILED
    ("eld ran but performed no link")."""
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    build_args = ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-", "vmlinux"]
    clean = clean_commands(dst, dst / "b", build_args)
    assert clean[0][-2:] == ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-"]
    assert "vmlinux" not in clean[0]
    # the build step still gets the target
    build = build_commands(dst, dst / "b", fake_eld, 2, [], build_args)
    assert build[-1][-3:] == build_args


def test_clean_and_build_target_coalescing_does_not_erase_the_real_link(tmp_path, fake_eld):
    """End-to-end regression, reproducing the exact real-kernel symptom.

    Before the fix, `clean_commands` forwarded a bare target name (e.g. the
    kernel's "vmlinux") to `make clean` too, turning it into `make clean
    vmlinux`: a real `make` treats that as "clean, then also build vmlinux",
    so the real link happens -- and gets logged -- during the *clean* phase.
    That log is then deleted (to reset it for "the real build") before the
    nominal build-phase `make ... vmlinux` runs, which finds vmlinux already
    up to date and links nothing. Net effect: a build that fully linked
    everything through eld was reported as CHECK FAILED ("eld ran but
    performed no link"). This fake models make's up-to-date behaviour (a
    target already on disk is not relinked) closely enough to tell the fixed
    and broken orderings apart.
    """
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)

    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld"):
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        if cmd[0] == "make":
            env = kw.get("env", {})
            out = Path(env["ELDCONV_LINK_LOG"]).parent / "app"
            if "vmlinux" in cmd[1:] and not out.exists():
                log = Path(env["ELDCONV_LINK_LOG"])
                log.parent.mkdir(parents=True, exist_ok=True)
                fake_elf(out)
                with open(log, "a") as fh:
                    fh.write("\x1f".join([str(log.parent), "-o", "app", "--gc-sections", "main.o"]) + "\x1f\n")
            return subprocess.CompletedProcess(cmd, 0, "built", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = run_check(dst, runner=runner, build_args=["vmlinux"])
    assert res.ok, res.problems
    assert res.invocations == 1


def test_build_args_reach_the_runner(tmp_path, fake_eld):
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=fake_eld)
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    run_check(dst, eld=fake_eld, runner=runner, probe=False, build_args=["ARCH=arm64"])
    assert any("ARCH=arm64" in c for c in seen)
