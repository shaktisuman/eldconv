"""The shim with response files and long link lines, `flags --eld`, and the generated
GN/Meson build scripts run twice or from another directory."""
import os
import shutil
import subprocess
import time

import pytest

from eldconv.checker import expand_response_files, parse_link_log
from eldconv.cli import _protect_flag_operands, build_parser
from eldconv.generator import SHIM_LD, convert_project


def _shim(tmp_path):
    """The shim in front of a fake ld.eld that prints its arguments and, for each response
    file, the file's lines."""
    eld = tmp_path / "echo-eld"
    eld.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; case "$a" in @*) '
                   'sed "s/^/  /" "${a#@}";; esac; done\n')
    eld.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(eld)))
    shim.chmod(0o755)
    return shim


def _run(shim, args, cwd, **env):
    e = dict(os.environ, **env)
    e.pop("ELDCONV_ELD", None)
    for k in ("ELDCONV_KEEP_GCC_PLUGIN", "ELDCONV_KEEP_Z_HINTS", "ELDCONV_LINK_LOG"):
        if k not in env:
            e.pop(k, None)
    return subprocess.run([str(shim), *args], capture_output=True, text=True, env=e, cwd=cwd,
                          check=True).stdout.splitlines()


GCC_RSP = ("-plugin\n/usr/libexec/gcc/x86_64-linux-gnu/13/liblto_plugin.so\n"
           "-plugin-opt=/usr/libexec/gcc/x86_64-linux-gnu/13/lto-wrapper\n"
           "-plugin-opt=-fresolution=/tmp/ccA.res\n-z\nrelro\n-z\nibt\n-zshstk\n-o\nprog\nm\\ x.o\n")


def test_filters_apply_inside_a_response_file(tmp_path):
    (tmp_path / "cc1.rsp").write_text(GCC_RSP)
    out = _run(_shim(tmp_path), ["@cc1.rsp"], tmp_path, TMPDIR=str(tmp_path))
    assert out[0].startswith("@") and out[0] != "@cc1.rsp"          # a filtered copy
    assert [x.strip() for x in out[1:]] == ["-z", "relro", "-o", "prog", "m\\ x.o"]
    assert not list(tmp_path.glob("eldconv.*")), "the filtered copy must be removed afterwards"
    assert (tmp_path / "cc1.rsp").read_text() == GCC_RSP             # the driver's own file is untouched


def test_response_file_with_nothing_to_filter_is_passed_as_is(tmp_path):
    (tmp_path / "x.rsp").write_text("-o\nprog\na.o\n")
    out = _run(_shim(tmp_path), ["@x.rsp", "b.o"], tmp_path, TMPDIR=str(tmp_path))
    assert out == ["@x.rsp", "  -o", "  prog", "  a.o", "b.o"]


def test_keep_switches_also_apply_inside_a_response_file(tmp_path):
    (tmp_path / "cc1.rsp").write_text(GCC_RSP)
    out = _run(_shim(tmp_path), ["@cc1.rsp"], tmp_path, TMPDIR=str(tmp_path),
               ELDCONV_KEEP_GCC_PLUGIN="1", ELDCONV_KEEP_Z_HINTS="1")
    assert out[0] == "@cc1.rsp"


def test_log_keeps_a_copy_of_the_response_file(tmp_path):
    (tmp_path / "x.rsp").write_text("-o\nprog\nm\\ x.o\n-plugin-opt=O2\n")
    log = tmp_path / "eld-link.log"
    _run(_shim(tmp_path), ["@x.rsp", "-lc"], tmp_path, ELDCONV_LINK_LOG=str(log))
    (tmp_path / "x.rsp").unlink()                    # the driver deletes its temporary file
    [inv] = parse_link_log(log)
    assert inv.args == ["-o", "prog", "m x.o", "-plugin-opt=O2", "-lc"]
    assert inv.output == "prog" and not inv.is_trycompile()


def test_link_through_gcc_response_file_is_judged_as_a_link(tmp_path):
    """gcc given `@file` hands collect2's arguments to ld as `@/tmp/ccXXXXXX`; before the
    shim kept a copy, `check` saw one bare argument and took the link for a configure probe."""
    if not shutil.which("gcc"):
        pytest.skip("needs gcc")
    eld = tmp_path / "eld"
    eld.write_text('#!/bin/sh\nexec ld "$@"\n')
    eld.chmod(0o755)
    shim_dir = tmp_path / "eld-bin"
    shim_dir.mkdir()
    (shim_dir / "ld").write_text(SHIM_LD.replace("@ELD@", str(eld)))
    (shim_dir / "ld").chmod(0o755)
    (tmp_path / "m.c").write_text("int main(void){return 0;}\n")
    subprocess.run(["gcc", "-c", "m.c"], cwd=tmp_path, check=True)
    (tmp_path / "link.rsp").write_text("m.o -o prog\n")
    log = tmp_path / "eld-link.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ELDCONV_")}
    subprocess.run(["gcc", f"-B{shim_dir}/", "@link.rsp"], cwd=tmp_path, check=True,
                   env=dict(env, ELDCONV_LINK_LOG=str(log)))
    [inv] = [i for i in parse_link_log(log) if not i.is_probe()]
    assert inv.output == "prog" and not inv.is_trycompile()
    assert "m.o" in inv.inputs()


def test_expand_response_files(tmp_path):
    (tmp_path / "a.rsp").write_text('-o "out file" @b.rsp')
    (tmp_path / "b.rsp").write_text("x.o\ny.o\n")
    (tmp_path / "loop.rsp").write_text("@loop.rsp")
    assert expand_response_files(["@a.rsp", "@missing.rsp", "@"], str(tmp_path)) == \
        ["-o", "out file", "x.o", "y.o", "@missing.rsp", "@"]
    assert expand_response_files(["@loop.rsp"], str(tmp_path)) == ["@loop.rsp"]   # bounded
    (tmp_path / "bad.rsp").write_text('-o "unterminated a.o')
    assert expand_response_files(["@bad.rsp"], str(tmp_path)) == ["-o", '"unterminated', "a.o"]


def test_long_link_line_costs_little(tmp_path):
    """Rebuilding the argument list once per argument took 7 s for 5000 arguments."""
    eld = tmp_path / "true-eld"
    eld.write_text("#!/bin/sh\nexit 0\n")
    eld.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(eld)))
    shim.chmod(0o755)
    args = ["-z", "relro", "-o", "app"] + [f"obj/dir/file{i:05d}.o" for i in range(5000)]
    log = tmp_path / "eld-link.log"
    t = time.monotonic()
    subprocess.run([str(shim), *args], check=True, env=dict(os.environ, ELDCONV_LINK_LOG=str(log)))
    assert time.monotonic() - t < 2.0
    [inv] = parse_link_log(log)
    assert inv.args == args


def test_flags_keeps_its_own_eld_option(tmp_path):
    for argv in (["flags", "--eld", "/opt/ld.eld", "-Wl,--icf=all"],
                 ["flags", "--eld=/opt/ld.eld", "-Wl,--icf=all"]):
        a = build_parser().parse_args(_protect_flag_operands(argv))
        assert a.eld == "/opt/ld.eld" and a.flags == ["-Wl,--icf=all"]


def _fake_tools(tmp_path, *names):
    """Tools that record their working directory and arguments."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for n in names:
        t = bin_dir / n
        t.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> "{tmp_path}/calls"\n')
        t.chmod(0o755)
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")


def test_gn_build_script_works_from_anywhere_and_twice(tmp_path):
    src = tmp_path / "s"
    (src / "build/toolchain").mkdir(parents=True)
    (src / ".gn").write_text('buildconfig = "//build/BUILDCONFIG.gn"\n')
    (src / "BUILD.gn").write_text('executable("app") { sources = [ "a.c" ] }\n')
    (src / "build/toolchain/BUILD.gn").write_text(
        'tool("link") {\n  command = "$ld -fuse-ld=lld -o {{output}} {{inputs}}"\n}\n')
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/x/ld.eld")
    env = _fake_tools(tmp_path, "gn", "ninja")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for _ in range(2):
        subprocess.run([str(dst / "build-eld.sh")], cwd=elsewhere, env=env, check=True)
    args = (dst / "out/eld/args.gn").read_text()
    assert args.count("use_lld = false") == 1
    calls = (tmp_path / "calls").read_text().splitlines()
    assert all(c.startswith(f"{dst}|") for c in calls), calls
    assert not (elsewhere / "out").exists()


def test_meson_build_script_reconfigures_on_a_second_run(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "meson.build").write_text("project('p', 'c')\nexecutable('app', 'a.c', "
                                     "link_args: ['-fuse-ld=lld'])\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/x/ld.eld")
    env = _fake_tools(tmp_path, "meson")
    subprocess.run([str(dst / "build-eld.sh")], env=env, check=True)
    (dst / "build-eld/meson-private").mkdir(parents=True)     # what the first setup leaves
    subprocess.run([str(dst / "build-eld.sh")], env=env, check=True)
    setups = [c.split("|", 1)[1] for c in (tmp_path / "calls").read_text().splitlines()
              if "|setup " in c]
    assert setups[0].startswith("setup ") and "--reconfigure" not in setups[0]
    assert setups[1].startswith("setup --reconfigure ")
