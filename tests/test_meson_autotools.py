"""Meson and Autotools support, and the checker's whole-tree view of what linked."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eldconv.checker import (LinkInvocation, build_commands, clean_commands, detect_output_system,
                             elf_notes_and_comment, linked_elsewhere, run_check)
from eldconv.converters.autotools import (LIBTOOL_PATCHED, PATCH_LIBTOOL_SH, patch_libtool_text,
                                          rewrite_configure_ac)
from eldconv.converters.meson import (inject_selection, is_machine_file, project_languages,
                                      rewrite_meson_text)
from eldconv.detect import detect
from eldconv.generator import convert_project


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ------------------------------------------------------------------ meson

def test_meson_strings_converted_and_emptied_elements_removed():
    text = ("project('m', 'c')\n"
            "add_project_link_arguments('-fuse-ld=lld', '-Wl,--icf=all', '-Wl,--gc-sections', language: 'c')\n"
            "executable('app', 'main.c', link_args: ['-Wl,--icf=safe'])\n"
            "x = ['-fuse-ld=lld']\n")
    new, d = rewrite_meson_text(text, "lld")
    lines = new.splitlines()
    assert lines[1] == "add_project_link_arguments('-Wl,--gc-sections', language: 'c')"
    assert lines[2] == "executable('app', 'main.c', link_args: [])"
    assert lines[3] == "x = []"
    assert {x.flag for x in d} >= {"-fuse-ld=lld", "--icf=all", "--icf=safe"}


def test_meson_sole_argument_removed_leaves_valid_call():
    new, _ = rewrite_meson_text("add_project_link_arguments('-fuse-ld=mold', language: 'cpp')\n", "mold")
    assert new.strip() == "add_project_link_arguments(language: 'cpp')"


def test_meson_concatenation_is_commented_out():
    new, d = rewrite_meson_text("args = ['-fuse-ld=' + get_option('ld')]\n", "lld")
    assert new.startswith("# eldconv: removed ->")


def test_meson_comments_and_unrelated_strings_untouched():
    text = "# '-fuse-ld=lld' is what we used to do\nmessage('use -fuse-ld=lld?')\n"
    new, _ = rewrite_meson_text(text, "lld")
    assert new.splitlines()[0] == text.splitlines()[0]


def test_meson_machine_file_linker_removed():
    text = "[binaries]\nc = 'gcc'\nc_ld = 'mold'\ncpp_ld = 'lld'\n\n[built-in options]\nc_link_args = ['-fuse-ld=mold', '-Wl,-O1']\n"
    assert is_machine_file(text)
    new, d = rewrite_meson_text(text, "mold", "native.ini")
    assert "# eldconv: removed -> c_ld = 'mold'" in new and "# eldconv: removed -> cpp_ld = 'lld'" in new
    assert "c = 'gcc'" in new


@pytest.mark.parametrize("text,langs", [
    ("project('x', 'c')", ["c"]),
    ("project('x', ['c', 'cpp'], version: '1')", ["c", "cpp"]),
    ("project('x', 'c', 'cpp', 'rust')", ["c", "cpp"]),
    ("project('x', language: ['cpp'])", ["cpp"]),
    ("project('x')\nadd_languages('c', 'fortran', required: false)", ["c", "fortran"]),
    ("project('x', 'c',\n  default_options: ['b_lto=true'])", ["c"]),
])
def test_meson_project_languages(text, langs):
    assert project_languages(text) == langs


def test_meson_injection_after_multiline_project_call():
    text = "project('x', 'c',\n  version: '1.0', # comment with ) paren\n  default_options: ['c_std=c11'])\nexecutable('a', 'a.c')\n"
    new, d = inject_selection(text, "/abs/eld-bin")
    head, tail = new.split("# eldconv: link with eld", 1)
    assert head.rstrip().endswith("default_options: ['c_std=c11'])")
    assert "'-B/abs/eld-bin/'" in tail and "executable('a', 'a.c')" in tail
    assert inject_selection(new, "/abs/eld-bin")[0] == new        # idempotent


def test_meson_detection_and_conversion(tmp_path):
    src = tmp_path / "s"
    write(src, "meson.build", "project('m', 'c')\nadd_project_link_arguments('-fuse-ld=lld', language: 'c')\n"
                              "subdir('sub')\n")
    write(src, "sub/meson.build", "executable('a', 'a.c', link_args: ['-Wl,--icf=all'])\n")
    write(src, "cross/arm.ini", "[binaries]\nc_ld = 'lld'\n")
    write(src, "setup.ini", "[pytest]\naddopts = -x\n")              # not a machine file
    info = detect(src)
    assert info.system == "meson" and info.linker == "lld"
    assert "setup.ini" not in {str(f.relative_to(src)) for f in info.files}
    rep = convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    top = (tmp_path / "d/meson.build").read_text()
    assert "# eldconv: link with eld" in top and str(tmp_path / "d/eld-bin") in top
    assert "--icf" not in (tmp_path / "d/sub/meson.build").read_text()
    assert "# eldconv: removed -> c_ld" in (tmp_path / "d/cross/arm.ini").read_text()
    assert (tmp_path / "d/build-eld.sh").exists()


HAVE_MESON = shutil.which("meson") and shutil.which("ninja") and shutil.which("cc")


@pytest.mark.skipif(not HAVE_MESON, reason="needs meson, ninja and cc")
def test_meson_e2e(tmp_path, e2e_eld):
    src = tmp_path / "s"
    write(src, "meson.build", "project('m', 'c')\nadd_project_link_arguments('-Wl,--gc-sections', language: 'c')\n"
                              "lib = shared_library('g', 'g.c')\n"
                              "executable('app', 'main.c', link_with: lib, link_args: ['-Wl,-O1'])\n")
    write(src, "g.c", "int g(void){return 42;}\n")
    write(src, "main.c", "int g(void);\nint main(void){return g()-42;}\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld=e2e_eld)
    res = run_check(dst, eld=e2e_eld, timeout=300)
    assert res.ok, (res.problems, res.stderr_tail)
    assert {Path(o["output"]).name for o in res.outputs if not o.get("trycompile")} >= {"app", "libg.so"}


# -------------------------------------------------------------- autotools

def test_configure_ac_case_patterns_are_left_alone():
    text = ('case "$LDFLAGS" in\n  *-fuse-ld=*) ;;\n  -fuse-ld=lld|-fuse-ld=gold) x=1 ;;\n'
            '  *) LDFLAGS="$LDFLAGS -fuse-ld=gold -Wl,--icf=all" ;;\nesac\n'
            'AX_CHECK_LINK_FLAG([-Wl,--gc-sections], [LDFLAGS="$LDFLAGS -Wl,--gc-sections"])\n')
    new, d = rewrite_configure_ac(text, "gold")
    lines = new.splitlines()
    assert lines[1] == "  *-fuse-ld=*) ;;"
    assert lines[2] == "  -fuse-ld=lld|-fuse-ld=gold) x=1 ;;"
    assert "-fuse-ld" not in lines[3] and "--icf" not in lines[3]
    assert lines[5] == 'AX_CHECK_LINK_FLAG([-Wl,--gc-sections], [LDFLAGS="$LDFLAGS -Wl,--gc-sections"])'


def test_libtool_patch():
    lt = "func_mode_link ()\n      -64|-mips[0-9]|-r[0-9][0-9]*|-xarch=*|-xtarget=*|+DA*|+DD*|-q*|-m*| \\\n"
    new = patch_libtool_text(lt)
    assert LIBTOOL_PATCHED in new
    assert patch_libtool_text(new) == new
    assert patch_libtool_text("no libtool here |-q*|-m*|") == "no libtool here |-q*|-m*|"


def test_libtool_sed_matches_python_patch(tmp_path):
    lt = write(tmp_path, "libtool", "#!/bin/sh\nfunc_mode_link ()\n  |-q*|-m*| \\\n")
    subprocess.run(["sh", "-c", PATCH_LIBTOOL_SH], cwd=tmp_path, check=True)
    subprocess.run(["sh", "-c", PATCH_LIBTOOL_SH], cwd=tmp_path, check=True)   # idempotent
    assert lt.read_text().count("-B*|") == 1 and LIBTOOL_PATCHED in lt.read_text()


def test_autotools_detection_and_conversion(tmp_path):
    src = tmp_path / "s"
    write(src, "configure.ac", 'AC_INIT([h],[1])\nLDFLAGS="$LDFLAGS -fuse-ld=gold"\nAC_OUTPUT\n')
    write(src, "Makefile.am", "bin_PROGRAMS = h\nh_LDFLAGS = -Wl,--icf=all -Wl,--as-needed\n")
    write(src, "build-aux/ltmain.sh", "func_mode_link ()\n  |-q*|-m*| \\\n")
    info = detect(src)
    assert info.system == "autotools" and info.linker == "gold"
    rep = convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    d = tmp_path / "d"
    assert "-fuse-ld" not in (d / "configure.ac").read_text()
    assert (d / "Makefile.am").read_text().endswith("h_LDFLAGS = -Wl,--as-needed\n")
    assert "include" not in (d / "Makefile.am").read_text()
    assert LIBTOOL_PATCHED in (d / "build-aux/ltmain.sh").read_text()
    script = (d / "build-eld.sh").read_text()
    assert "-B$SRC/eld-bin/" in script and "func_mode_link" in script
    assert not (d / "eld.mk").exists()


def test_autotools_build_commands(tmp_path):
    dst = tmp_path
    cmds = build_commands(dst, dst / "b", "/e/ld.eld", 3, [],
                          ["--host=aarch64-linux-gnu", "CC=aarch64-linux-gnu-gcc", "LDFLAGS=-L/x", "all"],
                          system="autotools")
    assert cmds[0] == ["autoreconf", "-fi"]
    assert cmds[1][0] == "./configure"
    assert cmds[1][1] == f"LDFLAGS=-B{dst}/eld-bin/ -fno-use-linker-plugin -L/x"
    assert "--host=aarch64-linux-gnu" in cmds[1] and "CC=aarch64-linux-gnu-gcc" in cmds[1]
    assert cmds[2][:2] == ["sh", "-c"]
    assert cmds[3] == ["make", "-j3", "all"]
    (dst / "configure").write_text("#!/bin/sh\n")
    assert build_commands(dst, dst / "b", "e", 1, [], system="autotools", toolchain="llvm")[0][1] \
        == f"LDFLAGS=-B{dst}/eld-bin/"


def test_meson_build_commands_and_clean(tmp_path):
    cmds = build_commands(tmp_path, tmp_path / "b", "e", 2, [], ["--cross-file=x.ini", "-Db_lto=true", "app"],
                          system="meson")
    assert cmds == [["meson", "setup", str(tmp_path / "b"), str(tmp_path), "--cross-file=x.ini", "-Db_lto=true"],
                    ["meson", "compile", "-C", str(tmp_path / "b"), "-j2", "app"]]
    assert clean_commands(tmp_path, tmp_path / "b", [], "meson") == []


def test_output_system_from_report_or_markers(tmp_path):
    assert detect_output_system(tmp_path, {"build": {"system": "meson"}}) == "meson"
    (tmp_path / "configure.ac").write_text("")
    assert detect_output_system(tmp_path) == "autotools"
    with pytest.raises(ValueError):
        detect_output_system(tmp_path / "empty")


HAVE_AUTOTOOLS = all(shutil.which(t) for t in ("autoreconf", "automake", "libtoolize", "make", "cc"))


@pytest.mark.skipif(not HAVE_AUTOTOOLS, reason="needs autoconf, automake, libtool, make, cc")
def test_autotools_libtool_e2e(tmp_path, e2e_eld):
    """The shared library must link through eld too, not just the program."""
    src = tmp_path / "s"
    write(src, "configure.ac", "AC_INIT([h],[1])\nAM_INIT_AUTOMAKE([foreign])\nAC_PROG_CC\nLT_INIT\n"
                               'LDFLAGS="$LDFLAGS -fuse-ld=gold"\nAC_CONFIG_FILES([Makefile])\nAC_OUTPUT\n')
    write(src, "Makefile.am", "lib_LTLIBRARIES = libg.la\nlibg_la_SOURCES = g.c\n"
                              "libg_la_LDFLAGS = -Wl,--gc-sections\nbin_PROGRAMS = h\nh_SOURCES = m.c\n"
                              "h_LDADD = libg.la\n")
    write(src, "g.c", "int g(void){return 42;}\n")
    write(src, "m.c", "int g(void);\nint main(void){return g()-42;}\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld=e2e_eld)
    res = run_check(dst, eld=e2e_eld, timeout=600)
    assert res.ok, (res.problems, res.stderr_tail)
    names = {Path(o["output"]).name for o in res.outputs if o.get("output") and not o.get("trycompile")}
    assert "h" in names and any(n.startswith("libg.so") for n in names)


# ------------------------------------------------- what linked, tree-wide

def test_probe_classification():
    assert LinkInvocation("/b", ["-o", "conftest", "conftest.o"]).is_trycompile()
    assert LinkInvocation("/b/build-eld/meson-private", ["-o", "x", "x.o"]).is_trycompile()
    assert LinkInvocation("/nonexistent", ["crt1.o", "conftest.o"]).is_trycompile()  # a.out probe
    assert not LinkInvocation("/b", ["-o", "app", "main.o"]).is_trycompile()


HAVE_CC_LLD = shutil.which("cc") and shutil.which("ld.lld")


@pytest.mark.skipif(not HAVE_CC_LLD, reason="needs cc and ld.lld")
def test_binaries_linked_by_another_linker_are_found(tmp_path):
    (tmp_path / "m.c").write_text("int main(void){return 0;}\n")
    subprocess.run(["cc", "-fuse-ld=lld", "-Wl,--build-id", "-o", str(tmp_path / "by_lld"),
                    str(tmp_path / "m.c")], check=True)
    subprocess.run(["cc", "-Wl,--build-id", "-o", str(tmp_path / "ours"), str(tmp_path / "m.c")], check=True)
    shutil.copy(tmp_path / "ours", tmp_path / "ours.stripped")       # derived copy: same build-id
    bid, comment = elf_notes_and_comment(tmp_path / "by_lld")
    assert bid and "LLD" in comment
    wrong, other = linked_elsewhere([tmp_path], 0, {tmp_path / "ours"})
    assert wrong == [str(tmp_path / "by_lld")]
    assert other == []


def test_elf_parser_survives_garbage(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"\x7fELF" + b"\xff" * 100)
    assert elf_notes_and_comment(p) == (None, "")
    assert elf_notes_and_comment(tmp_path / "missing") == (None, "")


@pytest.fixture
def e2e_eld(tmp_path):
    """The real ld.eld (ELDCONV_TEST_ELD) or a stand-in that forwards to the system ld."""
    real = os.environ.get("ELDCONV_TEST_ELD")
    if real:
        return real
    ld = shutil.which("ld.bfd") or shutil.which("ld")
    if not ld:
        pytest.skip("no system ld")
    p = tmp_path / "fake-eld" / "ld.eld"
    p.parent.mkdir()
    p.write_text(f'#!/bin/sh\ncase "$1" in --version) echo "fake eld";; --help) echo "  --gc-sections x";; '
                 f'*) exec {ld} "$@";; esac\n')
    p.chmod(0o755)
    return str(p)
