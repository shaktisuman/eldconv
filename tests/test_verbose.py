"""Verbose (-v / -vv) reporting."""
import io
import re

import pytest

from eldconv.checker import run_check
from eldconv.cli import main
from eldconv.detect import detect
from eldconv.generator import convert_project
from eldconv.log import QUIET, TRACE, VERBOSE, Log

STAMP = re.compile(r"^\[\s*\d+\.\d\ds\] ")


def mklog(level):
    buf = io.StringIO()
    return Log(level, stream=buf, color=False), buf


def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text(
        "LD = ld.gold\nLDFLAGS = -Wl,--icf=all -Wl,--gc-sections\napp:\n\t$(CC) $(LDFLAGS) -o $@ m.c\n")
    (src / "plain.mk").write_text("X = 1\n")
    return src


# ---------------------------------------------------------------- the Log object

def test_levels_gate_output():
    for level, want in ((QUIET, {"warn", "error"}),
                        (VERBOSE, {"step", "info", "warn", "error"}),
                        (TRACE, {"step", "info", "detail", "warn", "error"})):
        log, buf = mklog(level)
        log.step("step"); log.info("info"); log.detail("detail")
        log.warn("warn"); log.error("error")
        got = {w for w in ("step", "info", "detail", "warn", "error") if w in buf.getvalue()}
        assert got == want, (level, got)


def test_every_line_is_timestamped_and_prefixed():
    log, buf = mklog(TRACE)
    log.step("a"); log.info("b"); log.detail("c"); log.warn("d"); log.error("e")
    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 5
    for ln in lines:
        assert STAMP.match(ln), ln


def test_no_ansi_codes_when_color_is_off():
    log, buf = mklog(VERBOSE)
    log.step("x"); log.warn("y"); log.error("z")
    assert "\033[" not in buf.getvalue()


def test_color_is_used_when_requested():
    buf = io.StringIO()
    Log(VERBOSE, stream=buf, color=True).warn("careful")
    assert "\033[33m" in buf.getvalue()


def test_counts_and_summary():
    log, buf = mklog(VERBOSE)
    log.file_scanned("a", changed=True, n_diags=2)
    log.file_scanned("b", changed=False)
    log.file_scanned("c", changed=True)
    assert log.counts() == {"scanned": 3, "rewritten": 2, "unchanged": 1}
    log.summary()
    assert "scanned 3 build file(s): 2 rewritten, 1 unchanged" in buf.getvalue()
    assert "rewrote a (2 change(s))" in buf.getvalue()
    assert "unchanged b" not in buf.getvalue()  # detail level


def test_command_logging_includes_cwd():
    log, buf = mklog(VERBOSE)
    log.command(["ninja", "-C", "/b", "-j2"], cwd="/src")
    assert "run: ninja -C /b -j2" in buf.getvalue() and "(cwd /src)" in buf.getvalue()


def test_quiet_log_writes_nothing_for_progress():
    log, buf = mklog(QUIET)
    log.step("x"); log.info("y"); log.detail("z"); log.summary()
    log.file_scanned("f", changed=True)
    assert buf.getvalue() == ""


# ---------------------------------------------------------------- pipeline integration

def test_detect_explains_its_decision(tmp_path):
    src = project(tmp_path)
    log, buf = mklog(VERBOSE)
    detect(src, log=log)
    out = buf.getvalue()
    assert "detecting build system" in out
    assert "build system: make" in out
    assert "source linker: gold" in out
    assert "toolchain: gnu" in out
    assert "Makefile:1: make_ld -> gold" in out


def test_detect_trace_lists_every_file(tmp_path):
    src = project(tmp_path)
    log, buf = mklog(TRACE)
    detect(src, log=log)
    out = buf.getvalue()
    assert "scanning Makefile" in out and "scanning plain.mk" in out


def test_detect_reports_ambiguity_and_conditionals(tmp_path):
    (tmp_path / "Makefile").write_text(
        "ifneq ($(LLVM),)\nLD = ld.lld\nelse\nLD = ld\nendif\nall:\n")
    log, buf = mklog(VERBOSE)
    detect(tmp_path, log=log)
    out = buf.getvalue()
    assert "only conditional selections found" in out or "conditionals" in out
    assert "(assumed)" in out


def test_convert_streams_progress_and_diagnostics(tmp_path):
    src = project(tmp_path)
    log, buf = mklog(VERBOSE)
    convert_project(src, tmp_path / "dst", eld="/x/ld.eld", log=log)
    out = buf.getvalue()
    assert "copying" in out and "rewriting build files (source linker: gold)" in out
    assert "--icf=all" in out                      # the error, streamed
    assert "rewrote Makefile" in out
    assert "scanned 2 build file(s): 1 rewritten, 1 unchanged" in out
    assert "shim:" in out and "wrote eld.mk" in out
    assert "scanning for linker-identity gates" in out


def test_convert_quiet_by_default(tmp_path):
    src = project(tmp_path)
    log, buf = mklog(QUIET)
    convert_project(src, tmp_path / "dst", eld="/x/ld.eld", log=log)
    # only the things the user must know survive at level 0
    for line in buf.getvalue().splitlines():
        assert " ! " in line or " x " in line, line


def test_check_narrates_each_phase(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "build.ninja").write_text(
        "cc = gcc\nldflags = -fuse-ld=gold\nrule link\n  command = $cc $ldflags -o $out $in\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    log, buf = mklog(VERBOSE)
    import subprocess
    run_check(dst, eld=str(fake), probe=False, log=log,
              runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    out = buf.getvalue()
    assert "checking" in out and "build dir:" in out
    assert "run: ninja -C" in out and "== building" in out
    assert "CHECK FAILED" in out          # nothing linked: correctly reported
    assert "no link step went through eld" in out


# ---------------------------------------------------------------- CLI plumbing

@pytest.mark.parametrize("argv", [
    ["detect", "SRC"],
    ["convert", "SRC", "DST"],
    ["check", "DST"],
    ["flags", "-Wl,--gc-sections"],
])
def test_every_subcommand_accepts_v_and_vv(argv, tmp_path, capsys):
    from eldconv.cli import build_parser, _protect_flag_operands
    for flag in ("-v", "-vv", "--verbose"):
        a = build_parser().parse_args(_protect_flag_operands(argv + [flag]))
        assert a.verbose >= 1


def test_cli_verbose_goes_to_stderr_not_stdout(tmp_path, capsys):
    src = project(tmp_path)
    assert main(["detect", str(src), "-v"]) == 0
    cap = capsys.readouterr()
    import json
    json.loads(cap.out)                   # stdout stays valid JSON
    assert "detecting build system" in cap.err


def test_cli_convert_does_not_duplicate_diagnostics(tmp_path, capsys):
    """With -v the diagnostics stream as they happen; do not also dump them at the end."""
    src = project(tmp_path)
    main(["convert", str(src), str(tmp_path / "d1"), "-v"])
    verbose_err = capsys.readouterr().err
    assert verbose_err.count("--icf=all") == 1

    main(["convert", str(src), str(tmp_path / "d2")])
    plain_err = capsys.readouterr().err
    assert plain_err.count("--icf=all") == 1
    assert "detecting build system" not in plain_err


def test_cli_flags_still_parses_dashed_operands_with_verbose(capsys):
    assert main(["flags", "-Wl,--icf=all", "--source-linker", "lld", "-v"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == ""
    assert "converting" in cap.err and "identical-code-folding" in cap.err
