"""The check's console and Markdown reports (report.py), and how the CLI uses them: the
report by default, JSON with --json, the comparison with the original build by default."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from eldconv import cli
from eldconv.checker import CHECK_MARKDOWN, CheckResult, run_check
from eldconv.generator import convert_project
from eldconv.report import check_markdown, check_text, compare_markdown, compare_text

CC = shutil.which("cc") or shutil.which("gcc")
needs_build = pytest.mark.skipif(not (CC and shutil.which("ld") and shutil.which("make")),
                                 reason="needs cc, ld and make")

LAYOUT = {
    "reference": {"segments": [{"type": "LOAD", "flags": "R", "offset": 0, "vaddr": 0, "paddr": 0,
                                "filesz": 16, "memsz": 16, "align": 4096, "sections": [".rodata"]},
                               {"type": "LOAD", "flags": "RX", "offset": 4096, "vaddr": 4096,
                                "paddr": 4096, "filesz": 32, "memsz": 32, "align": 4096,
                                "sections": [".text"]}],
                  "sections": [{"name": ".rodata", "addr": 0, "size": 16, "flags": "A", "lma": 0,
                                "segment": "R"},
                               {"name": ".text", "addr": 4096, "size": 32, "flags": "AX", "lma": 4096,
                                "segment": "RX"}]},
    "eld": {"segments": [{"type": "LOAD", "flags": "RX", "offset": 0, "vaddr": 0, "paddr": 0,
                          "filesz": 64, "memsz": 64, "align": 4096, "sections": [".rodata", ".text"]}],
            "sections": [{"name": ".rodata", "addr": 0, "size": 16, "flags": "A", "lma": 0, "segment": "RX"},
                         {"name": ".text", "addr": 32, "size": 40, "flags": "AX", "lma": 32,
                          "segment": "RX"}]},
}


def result(ok=False):
    root = "/w/dst"
    return {
        "ok": ok, "eld": "/opt/ld.eld", "eld_version": "eld 24.0", "build_dir": f"{root}/build-eld",
        "build_log": f"{root}/build-eld/eldconv-build.log", "invocations": 3,
        "reference": {"dir": "/w/dst.reference", "build_log": "/w/ref.log", "returncode": 0, "compared": 2},
        "outputs": [
            {"output": f"{root}/build-eld/app", "exists": True, "elf": True, "elf_type": "DYN",
             "reference": "/w/dst.reference/build-eld/app", "script": True, "layout": LAYOUT,
             "validation": [{"level": "warn", "what": "stack", "message": "no PT_GNU_STACK"}],
             "differences": [{"level": "error", "what": "stack", "message": "stack is executable now"},
                             {"level": "info", "what": "order", "message": "1 section(s) moved"},
                             {"level": "warn", "what": "needed", "message": "now also needs libx"}]},
            {"output": f"{root}/build-eld/libok.so", "exists": True, "elf": True, "elf_type": "DYN",
             "reference": "/w/dst.reference/build-eld/libok.so", "differences": []},
            {"output": f"{root}/build-eld/probe", "trycompile": True, "elf": True, "elf_type": "EXEC"},
        ],
        "problems": [f"{root}/build-eld/app: stack is executable now (compared with x)",
                     "eld did not recognise these flags: --foo"],
        "warnings": [f"{root}/build-eld/app: now also needs libx (compared with x)"],
    }


def test_console_report_lists_what_needs_attention():
    text = check_text(result(), Path("/w/dst"), files=["/w/dst/build-eld/eldconv-check.md"])
    assert text.splitlines()[0].startswith("eldconv check FAILED")
    assert "2 outputs compared with the original build" in text.splitlines()[0]
    assert "build-eld/app" in text and "build-eld/libok.so" in text and "probe" not in text
    assert "1 error, 1 warning" in text and "no differences" in text
    # the comparison's error says more than validation's warning about the same thing
    assert "stack is executable now" in text and "no PT_GNU_STACK" not in text
    assert text.index("stack is executable now") < text.index("now also needs libx")   # errors first
    assert "1 section(s) moved" not in text and "1 informational difference" in text
    # per-output problems are listed under the output, not again under Problems
    general = text[text.index("Problems"):]
    assert "--foo" in general and "compared with x" not in general
    assert "/w/dst/build-eld/eldconv-check.md" in text and text.rstrip().endswith("CHECK FAILED")
    assert "\033[" not in text


def test_console_report_verbose_and_color():
    text = check_text(result(ok=True), Path("/w/dst"), color=True, verbose=True)
    assert "1 section(s) moved" in text and "\033[32m" in text and "CHECK PASSED" in text


def test_console_report_without_a_comparison():
    r = result()
    r["reference"] = None
    for o in r["outputs"]:
        o.pop("differences", None)
    text = check_text(r, Path("/w/dst"))
    assert "not compared with the original build" in text and "not compared" in text


def test_markdown_report_has_everything():
    md = check_markdown(result(), Path("/w/dst"))
    assert md.startswith("# eldconv check: FAILED")
    assert "| `build-eld/app` | DYN (linker script) | ok | 1 error, 1 warning, 1 info |" in md
    assert "## `build-eld/app`" in md and "placed by a linker script" in md
    assert "1 section(s) moved" in md and "🔴 error" in md
    assert "<details><summary>Layout" in md and "**Segments, reference**" in md
    assert "| `.text` | `0x1000` | 32 | RX | `0x20` | 40 | RX | +8 |" in md
    assert "eld did not recognise these flags: --foo" in md


def test_compare_reports():
    found = [{"level": "warn", "what": "permissions", "message": "rodata is executable"},
             {"level": "info", "what": "order", "message": "moved"}]
    text = compare_text("ref.so", "new.so", found, LAYOUT)
    assert "rodata is executable" in text and "moved" not in text and "COMPARE: 1 warning" in text
    seg = text[text.index("Segments"):]
    rows = seg.splitlines()[2:4]                     # two LOADs in the reference, one in eld's
    assert len(rows) == 2 and rows[1].rstrip().endswith("LOAD         -")
    md = compare_markdown("ref.so", "new.so", found, LAYOUT)
    assert md.startswith("# eldconv compare: 1 warning, 1 info") and "<details open>" in md


# ------------------------------------------------------------------------------- CLI

def _fake_check(seen):
    def fake_run_check(dst, **kw):
        seen.update(kw)
        return CheckResult(ok=True, eld="/x/ld.eld", build_dir=str(Path(dst) / "build-eld"))
    return fake_run_check


def test_check_prints_the_report_and_json_on_request(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli, "run_check", _fake_check(seen))
    assert cli.main(["check", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "eldconv check PASSED" in out and CHECK_MARKDOWN in out
    assert seen["compare"] is None                                    # compared when it can be
    assert cli.main(["check", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out[:out.rindex("CHECK")])["ok"] is True
    cli.main(["check", str(tmp_path), "--compare"])
    assert seen["compare"] is True
    cli.main(["check", str(tmp_path), "--no-compare"])
    assert seen["compare"] is False
    with pytest.raises(SystemExit):
        cli.main(["check", str(tmp_path), "--compare", "--no-compare"])


def test_convert_check_compares_by_default(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli, "run_check", _fake_check(seen))
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("all:\n\t$(CC) -Wl,--gc-sections -o app main.c\n")
    assert cli.main(["convert", str(src), str(tmp_path / "dst"), "--eld", "/bin/true", "-q", "--check"]) == 0
    assert seen["compare"] is None


@needs_build
def test_compare_command_report_file(tmp_path, capsys):
    (tmp_path / "m.c").write_text("int main(void){return 0;}\n")
    for name in ("a", "b"):
        subprocess.run([CC, "m.c", "-o", name], cwd=tmp_path, check=True)
    rep = tmp_path / "cmp.md"
    assert cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b"), "--report", str(rep)]) == 0
    out = capsys.readouterr().out
    assert "COMPARE: no differences" in out and "Segments" in out
    assert rep.read_text().startswith("# eldconv compare:")
    assert cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b"), "--json"]) == 0
    assert "layout" in json.loads(capsys.readouterr().out)


# ------------------------------------------------------------- comparing by default

def _project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("all: app\napp: main.c\n\t$(CC) -o $@ main.c\nclean:\n\trm -f app\n")
    (src / "main.c").write_text("int main(void){return 0;}\n")
    eld = tmp_path / "ld.eld"
    eld.write_text('#!/bin/sh\ncase "$1" in --help|--version) echo eld; exit 0;; esac\nexec ld "$@"\n')
    eld.chmod(0o755)
    dst = tmp_path / "dst"
    convert_project(src, dst, eld=str(eld))
    return src, dst, eld


@needs_build
def test_default_comparison_runs_and_writes_the_markdown_report(tmp_path):
    src, dst, eld = _project(tmp_path)
    res = run_check(dst, eld=str(eld), probe=False, compare=None)
    assert res.ok, res.problems
    assert res.reference["compared"] == 1
    [app] = [o for o in res.outputs if o.get("differences") is not None]
    assert set(app["layout"]) == {"reference", "eld"}
    md = (Path(res.build_dir) / CHECK_MARKDOWN).read_text()
    assert "# eldconv check: PASSED" in md and "Layout" in md


@needs_build
def test_default_comparison_without_the_source_is_a_warning(tmp_path):
    src, dst, eld = _project(tmp_path)
    shutil.rmtree(src)
    res = run_check(dst, eld=str(eld), probe=False, compare=None)
    assert res.ok, res.problems
    assert any("compare with the original build: the source tree" in w for w in res.warnings)
    res = run_check(dst, eld=str(eld), probe=False, compare=True)
    assert not res.ok and any("--compare: the source tree" in p for p in res.problems)
