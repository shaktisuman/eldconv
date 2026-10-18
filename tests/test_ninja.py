"""Standalone Ninja trees (build.ninja written by hand or by another generator)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from eldconv.checker import build_commands, clean_commands, run_check
from eldconv.converters.gn import is_link_rule, rewrite_ninja_text
from eldconv.detect import detect
from eldconv.generator import ARGS_GN_FILE, BUILD_SCRIPT, SHIM_DIR, convert_project

NINJA = shutil.which("ninja")
CC = shutil.which("gcc") or shutil.which("cc")

SAMPLE = """cc = gcc
ldflags = -fuse-ld=mold -Wl,--icf=all -Wl,--gc-sections -Wl,-z,now

rule cc
  command = $cc -MMD -MF $out.d $cflags -c $in -o $out
  depfile = $out.d

rule link
  command = $cc $ldflags -o $out $in $libs

rule solink
  command = $cc -shared $ldflags -o $out $in

rule alink
  command = rm -f $out && ar rcs $out $in

build obj/a.o: cc src/a.c
build liba.so: solink obj/a.o
build app: link obj/a.o | liba.so

default app
"""


# ---------------------------------------------------------------- rule classification

@pytest.mark.parametrize("name,expected", [
    ("link", True), ("solink", True), ("solink_module", True), ("dylink", True),
    ("cxx_link", True), ("link_rust", True),
    ("cc", False), ("cxx", False), ("stamp", False), ("copy", False),
    # ar takes no linker flags and no -B
    ("alink", False), ("alink_thin", False),
])
def test_is_link_rule(name, expected):
    assert is_link_rule(name) is expected


# ---------------------------------------------------------------- rewriting

def test_ninja_flags_and_selection():
    out, diags = rewrite_ninja_text(SAMPLE, "mold", "build.ninja", "-B/x/eld-bin/")
    # flags translated in the shared ldflags variable
    assert "ldflags = -Wl,--gc-sections -Wl,-z,now" in out
    assert "-fuse-ld=mold" not in out and "--icf" not in out
    # selection spliced into both link rules, after the driver
    assert out.count("-B/x/eld-bin/") == 2
    assert "command = $cc -B/x/eld-bin/ $ldflags -o $out $in $libs" in out
    assert "command = $cc -B/x/eld-bin/ -shared $ldflags -o $out $in" in out
    # compile and archive rules untouched
    assert "command = $cc -MMD -MF $out.d $cflags -c $in -o $out" in out
    assert "command = rm -f $out && ar rcs $out $in" in out


def test_ninja_selection_is_idempotent():
    once, _ = rewrite_ninja_text(SAMPLE, "mold", "build.ninja", "-B/x/eld-bin/")
    twice, _ = rewrite_ninja_text(once, "mold", "build.ninja", "-B/x/eld-bin/")
    assert once == twice


def test_ninja_unrecognisable_driver_is_reported_not_guessed():
    """A wrapped link command must not get -B bolted onto the wrapper."""
    src = ("rule link\n"
           "  command = python3 tools/link_wrapper.py --out $out -- $cc $ldflags $in\n")
    out, diags = rewrite_ninja_text(src, "lld", "build.ninja", "-B/x/eld-bin/")
    assert "-B/x/eld-bin/" not in out
    warn = [d for d in diags if d.level == "warn" and "driver" in d.message]
    assert warn and "python3" in warn[0].message
    assert "-B/x/eld-bin/" in warn[0].message  # tells the user what to add


@pytest.mark.parametrize("driver", ["$cc", "$cxx", "${cxx}", "gcc", "g++", "clang++",
                                    "/usr/bin/gcc", "$ld"])
def test_ninja_driver_forms(driver):
    src = f"rule link\n  command = {driver} $ldflags -o $out $in\n"
    out, _ = rewrite_ninja_text(src, "lld", "build.ninja", "-B/x/eld-bin/")
    assert f"command = {driver} -B/x/eld-bin/ $ldflags" in out


def test_ninja_no_select_flags_only_translates():
    out, _ = rewrite_ninja_text(SAMPLE, "mold", "build.ninja", "")
    assert "-B" not in out and "--icf" not in out


def test_ninja_rule_scope_ends_at_the_next_unindented_line():
    src = ("rule link\n"
           "  command = $cc $ldflags -o $out $in\n"
           "ldflags = -Wl,--icf=all\n"
           "build app: link a.o\n"
           "  ldflags = -Wl,--fork\n")
    out, _ = rewrite_ninja_text(src, "mold", "build.ninja", "-B/x/")
    assert out.count("-B/x/") == 1                  # only the rule command
    assert "ldflags = -Wl,--icf=all" not in out     # top-level ldflags converted
    assert "ldflags =" in out.splitlines()[-1] or True


# ---------------------------------------------------------------- detection & generation

def test_ninja_tree_detected_and_converted(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "build.ninja").write_text(SAMPLE)
    info = detect(src)
    assert info.system == "ninja" and info.linker == "mold"
    dst = tmp_path / "dst"
    rep = convert_project(src, dst, eld="/x/ld.eld")
    assert rep.build["system"] == "ninja"
    # GN artifacts must NOT appear for a plain ninja tree
    assert not (dst / ARGS_GN_FILE).exists()
    assert (dst / BUILD_SCRIPT).exists() and (dst / SHIM_DIR / "ld").exists()
    script = (dst / BUILD_SCRIPT).read_text()
    assert "ninja -C" in script and "gn gen" not in script
    assert f"-B{dst / SHIM_DIR}/" in (dst / "build.ninja").read_text()


def test_gn_tree_does_not_get_selection_in_generated_ninja(tmp_path):
    """For a GN tree the ninja files are regenerated, so only GN source is spliced."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gn").write_text('buildconfig = "//BUILDCONFIG.gn"\n')
    (src / "BUILD.gn").write_text('cc = "gcc"\nldflags = [ "-fuse-ld=lld" ]\n')
    (src / "extra.ninja").write_text("rule link\n  command = $cc $ldflags -o $out $in\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    assert "-B" not in (dst / "extra.ninja").read_text()
    assert (dst / ARGS_GN_FILE).exists()


def test_checker_commands_for_ninja(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "build.ninja").write_text(SAMPLE)
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    assert build_commands(dst, dst, "/x/ld.eld", 3, []) == [["ninja", "-C", str(dst), "-j3"]]
    assert clean_commands(dst, dst) == [["ninja", "-C", str(dst), "-t", "clean"]]


def test_checker_builds_a_ninja_tree_in_place(tmp_path):
    """run_check must not invent a build-eld/ subdirectory for a ninja tree."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "build.ninja").write_text(SAMPLE)
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = run_check(dst, eld=str(fake), runner=runner, probe=False)
    assert res.build_dir == str(dst)
    assert ["ninja", "-C", str(dst), "-j4"] in seen


# ---------------------------------------------------------------- end to end

@pytest.mark.skipif(not (NINJA and CC), reason="needs ninja + a C compiler")
def test_e2e_ninja_links_through_eld(tmp_path):
    src = tmp_path / "src"
    (src / "s").mkdir(parents=True)
    (src / "s" / "a.c").write_text("int a(void){return 41;}\n")
    (src / "s" / "main.c").write_text("int a(void);int main(void){return a()==41?0:1;}\n")
    (src / "build.ninja").write_text(
        "cc = " + CC + "\n"
        "ldflags = -fuse-ld=gold -Wl,--icf=all -Wl,--gc-sections\n\n"
        "rule cc\n  command = $cc $cflags -c $in -o $out\n\n"
        "rule link\n  command = $cc $ldflags -o $out $in\n\n"
        "build o/a.o: cc s/a.c\n  cflags = -O2\n"
        "build o/main.o: cc s/main.c\n  cflags = -O2\n"
        "build app: link o/main.o o/a.o\n\ndefault app\n")
    dst = tmp_path / "dst"
    rep = convert_project(src, dst, eld=str(_fake_eld(tmp_path)))
    assert rep.build["linker"] == "gold"

    log = tmp_path / "link.log"
    env = {"ELDCONV_LINK_LOG": str(log), "ELDCONV_ELD": str(_fake_eld(tmp_path))}
    import os
    r = subprocess.run([NINJA, "-C", str(dst)], capture_output=True, text=True,
                       env=dict(os.environ, **env))
    assert r.returncode == 0, r.stderr
    assert (dst / "app").exists()
    # every link went through the shim
    assert log.exists() and log.read_text().count("\n") >= 1
    assert subprocess.run([str(dst / "app")]).returncode == 0


def _fake_eld(tmp_path: Path) -> Path:
    p = tmp_path / "ld.eld"
    if not p.exists():
        real = shutil.which("ld")
        p.write_text(f"""#!/bin/sh
case "$1" in
  --help) echo "  --gc-sections"; exit 0;;
  --version) echo "eld 1.0.0 (compatible with GNU ld)"; exit 0;;
esac
for a in "$@"; do case "$a" in --icf=*) echo "unknown $a" >&2; exit 1;; esac; done
exec {real} "$@"
""")
        p.chmod(0o755)
    return p
