"""`check` keeps the whole build output, streams it at -v, and `convert --check` takes
every option `check` does."""
import io
import os
import subprocess
from pathlib import Path

import pytest

from eldconv import cli
from eldconv.checker import BUILD_LOG, _run_streaming, default_jobs, run_check
from eldconv.generator import convert_project
from eldconv.log import VERBOSE, Log


def _make_tree(tmp_path, recipe="@echo building; echo to-stderr >&2"):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text(f"LDFLAGS = -fuse-ld=gold\nall:\n\t{recipe}\nclean:\n\t@true\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    return dst, fake


def test_build_output_goes_to_a_log_file(tmp_path):
    dst, fake = _make_tree(tmp_path)
    res = run_check(dst, eld=str(fake), probe=False, pre_build=["echo configured"])
    log = Path(res.build_log)
    assert log == dst / "build-eld" / BUILD_LOG
    text = log.read_text()
    assert "$ sh -c echo configured\nconfigured\n[exit 0]\n" in text
    assert "building" in text and "to-stderr" in text
    assert res.as_dict()["build_log"] == str(log)


def test_build_output_is_streamed_at_v(tmp_path):
    dst, fake = _make_tree(tmp_path)
    buf = io.StringIO()
    run_check(dst, eld=str(fake), probe=False, log=Log(VERBOSE, stream=buf, color=False))
    assert "   | building" in buf.getvalue()


def test_a_fake_runner_still_fills_the_log(tmp_path):
    dst, fake = _make_tree(tmp_path)
    res = run_check(dst, eld=str(fake), probe=False,
                    runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "out\n", "boom\n"))
    text = Path(res.build_log).read_text()
    assert "out\nboom\n[exit 2]" in text


def test_streaming_runner_collects_output_and_times_out(tmp_path):
    sink = io.StringIO()
    lines = []
    r = _run_streaming(["sh", "-c", "echo a; echo b >&2; exit 3"], dict(os.environ), str(tmp_path),
                       None, sink, lines.append)
    assert (r.returncode, r.stdout, r.stderr) == (3, "a\n", "b\n")
    assert sorted(lines) == ["a", "b"] and sorted(sink.getvalue().splitlines()) == ["a", "b"]
    with pytest.raises(subprocess.TimeoutExpired) as e:
        _run_streaming(["sh", "-c", "echo started; exec sleep 30"], dict(os.environ), str(tmp_path),
                       1, io.StringIO(), None)
    assert e.value.stdout == "started\n"


def test_timeout_is_recorded_in_the_log(tmp_path):
    dst, fake = _make_tree(tmp_path, recipe="@echo slow; sleep 30")
    res = run_check(dst, eld=str(fake), probe=False, timeout=1)
    text = Path(res.build_log).read_text()
    assert "slow" in text and "[timed out after 1s]" in text
    assert res.returncode == 124


def test_convert_check_takes_the_check_options(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("LDFLAGS = -fuse-ld=gold\nall:\n\t@true\n")
    seen = {}

    def fake_run_check(dst, **kw):
        seen.update(kw)
        from eldconv.checker import CheckResult
        return CheckResult(ok=True, eld="/x/ld.eld")
    monkeypatch.setattr(cli, "run_check", fake_run_check)
    rc = cli.main(["convert", str(src), str(tmp_path / "dst"), "--eld", "/x/ld.eld", "--check",
                   "--pre-build", "./configure", "--build-dir", "out", "--no-probe",
                   "--allow-unresolved-data", "--allow-ignored-version-scripts", "--no-clean",
                   "--build-arg", "ARCH=arm64", "--timeout", "60", "-j", "3"])
    assert rc == 0
    assert seen["pre_build"] == ["./configure"] and seen["build_dir"] == Path("out")
    assert seen["probe"] is False and seen["clean"] is False
    assert seen["allow_unresolved_data"] and seen["allow_ignored_version_scripts"]
    assert seen["build_args"] == ["ARCH=arm64"] and seen["timeout"] == 60 and seen["jobs"] == 3


def test_jobs_default_to_the_cpu_count(tmp_path, monkeypatch):
    seen = {}

    def fake_run_check(dst, **kw):
        seen.update(kw)
        from eldconv.checker import CheckResult
        return CheckResult(ok=True, eld="/x/ld.eld")
    monkeypatch.setattr(cli, "run_check", fake_run_check)
    cli.main(["check", str(tmp_path)])
    assert seen["jobs"] == default_jobs() == (os.cpu_count() or 4)
