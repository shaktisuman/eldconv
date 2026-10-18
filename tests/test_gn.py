"""GN / Ninja conversion — the build system Chromium uses."""
import shutil
from pathlib import Path

import pytest

from eldconv.converters.gn import rewrite_gn_text, rewrite_ninja_text
from eldconv.detect import classify_file, detect
from eldconv.generator import ARGS_GN_FILE, SHIM_DIR, convert_project

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- detection

def test_gn_tree_is_detected(tmp_path):
    (tmp_path / ".gn").write_text('buildconfig = "//build/config/BUILDCONFIG.gn"\n')
    (tmp_path / "BUILD.gn").write_text('executable("app") { sources = [ "main.cc" ] }\n')
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "config.gni").write_text(
        'use_lld = true\nldflags = [ "-fuse-ld=lld" ]\ncc = "clang"\n')
    info = detect(tmp_path)
    assert info.system == "gn"
    assert info.linker == "lld"
    assert info.toolchain == "llvm"


def test_gn_beats_ninja_when_both_present(tmp_path):
    (tmp_path / "BUILD.gn").write_text('ldflags = [ "-fuse-ld=lld" ]\n')
    (tmp_path / "build.ninja").write_text("rule link\n  command = clang -fuse-ld=lld\n")
    assert detect(tmp_path).system == "gn"


def test_plain_ninja_tree_is_detected(tmp_path):
    (tmp_path / "build.ninja").write_text(
        "rule link\n  command = clang $ldflags -o $out $in\nldflags = -fuse-ld=mold\n")
    info = detect(tmp_path)
    assert info.system == "ninja" and info.linker == "mold"


def test_classify_file():
    assert classify_file(Path("BUILD.gn")) == "gn"
    assert classify_file(Path("build/config/compiler/BUILD.gn")) == "gn"
    assert classify_file(Path("x.gni")) == "gn"
    assert classify_file(Path("build.ninja")) == "ninja"
    assert classify_file(Path("CMakeLists.txt")) == "cmake"
    assert classify_file(Path("Makefile")) == "make"


def test_unknown_build_system_admits_it(tmp_path):
    """Regression: a Bazel/meson tree was reported as gnu + bfd with confidence."""
    (tmp_path / "BUILD.bazel").write_text('cc_binary(name = "app")\n')
    info = detect(tmp_path)
    assert info.system == "unknown"
    assert info.toolchain == "unknown" and info.linker == "unknown"
    assert info.linker_assumed and info.ambiguous


# ---------------------------------------------------------------- GN rewriting

def test_gn_list_elements_convert_independently():
    src = '  ldflags += [ "-Wl,--icf=all", "-Wl,--gc-sections", "-fuse-ld=lld" ]'
    out, diags = rewrite_gn_text(src, "lld")
    assert out == '  ldflags += [ "-Wl,--gc-sections" ]'
    assert {d.flag for d in diags} >= {"--icf=all", "-fuse-ld=lld"}


def test_gn_list_emptied_completely_becomes_empty_list():
    out, _ = rewrite_gn_text('      ldflags += [ "-Wl,--time-trace" ]', "lld")
    assert out == "      ldflags += []"


def test_gn_use_lld_is_pinned_false():
    out, diags = rewrite_gn_text("        use_lld = true\n  use_mold = true\n", "lld")
    assert "use_lld = false" in out and "use_mold = false" in out
    assert "# eldconv: was true" in out
    assert {d.flag for d in diags} == {"use_lld", "use_mold"}


def test_gn_linker_assert_is_removed():
    out, diags = rewrite_gn_text('    assert(use_lld, "LTO is only supported with lld")', "lld")
    assert out.lstrip().startswith("# eldconv: removed")
    assert diags[0].flag == "assert" and diags[0].level == "warn"


@pytest.mark.parametrize("src", [
    '      ldflags += [ "-Wl,--version-script=" + rebase_path(f, root_build_dir) ]',
    '  swiftmodule_switch = "-Wl,-add_ast_path,"',
    '      ldflags += [ "-Wl,-sectcreate,__TEXT,__entitlements," + x ]',
])
def test_gn_concatenation_prefixes_keep_their_separator(src):
    """Regression: `"--x="` + value lost the '=', and `"-Wl,a,"` lost the ','."""
    out, diags = rewrite_gn_text(src, "lld")
    assert out == src, out
    assert not [d for d in diags if d.level == "error"]


def test_gn_selection_flag_in_a_concatenation_removes_the_statement():
    """`"--ld-path=" + rebase_path(...)` cannot lose just the flag: the path
    would be left behind as a bare ldflags entry."""
    src = '      ldflags += [ "--ld-path=" + rebase_path(linker_path, root_build_dir) ]'
    out, diags = rewrite_gn_text(src, "lld")
    assert out.lstrip().startswith("# eldconv: removed ->")
    assert "rebase_path" not in out.split("->")[0]
    assert any("concatenation" in d.message for d in diags)


def test_gn_non_flag_strings_are_untouched():
    for src in ['  sources = [ "main.cc", "util.cc" ]',
                '  deps = [ "//base:base", "//third_party/icu" ]',
                '  output_name = "chrome"',
                '  args = [ "--enable-features=Foo" ]']:
        out, diags = rewrite_gn_text(src, "lld")
        assert out == src, src


def test_gn_dyld_placeholders_are_not_response_files():
    """Regression: @loader_path / @executable_path were reported as response files."""
    src = '      ldflags += [ "-Wl,-rpath,@loader_path/../Frameworks", "-Wl,-rpath,@executable_path/." ]'
    out, diags = rewrite_gn_text(src, "lld")
    assert out == src
    assert not [d for d in diags if d.flag.startswith("@")]


def test_gn_real_response_file_is_still_reported():
    src = '  command = "$ld $ldflags -o $out @$rspfile"'
    out, diags = rewrite_gn_text(src, "lld")
    assert any(d.level == "error" and "response file" in d.message for d in diags)


def test_gn_build_id_without_a_value_is_not_an_error():
    """Regression: `--build-id` is legal on its own; it was flagged as missing a value."""
    out, diags = rewrite_gn_text('    ldflags += [ "-Wl,--build-id" ]', "lld")
    assert out == '    ldflags += [ "-Wl,--build-id" ]'
    assert not [d for d in diags if d.level == "error"]


# ---------------------------------------------------------------- ninja rewriting

def test_ninja_link_rule_is_converted():
    src = ("rule link\n"
           "  command = clang++ $ldflags -Wl,--icf=all -o $out $in\n"
           "rule cxx\n"
           "  command = clang++ $cflags --icf=all -c $in -o $out\n")
    out, diags = rewrite_ninja_text(src, "lld")
    assert "--icf=all -o $out" not in out           # link rule converted
    assert "$cflags --icf=all -c" in out            # compile rule untouched
    assert any(d.flag == "--icf=all" for d in diags)


def test_ninja_ldflags_variable_is_converted():
    out, _ = rewrite_ninja_text("ldflags = -Wl,--icf=all -Wl,--gc-sections\n", "lld")
    assert out.strip() == "ldflags = -Wl,--gc-sections"


# ---------------------------------------------------------------- generation

def test_convert_gn_project_generates_args_gn(tmp_path):
    src = tmp_path / "src"
    (src / "build" / "config").mkdir(parents=True)
    (src / ".gn").write_text('buildconfig = "//build/config/BUILDCONFIG.gn"\n')
    (src / "BUILD.gn").write_text('executable("app") { sources = [ "main.cc" ] }\n')
    (src / "build" / "config" / "compiler.gni").write_text(
        'cc = "clang"\nuse_lld = true\nldflags = [ "-fuse-ld=lld", "-Wl,--icf=all" ]\n')
    dst = tmp_path / "dst"
    rep = convert_project(src, dst, eld="/x/ld.eld")
    assert rep.build["system"] == "gn"
    args = (dst / ARGS_GN_FILE).read_text()
    assert "use_lld = false" in args and "use_mold = false" in args
    assert (dst / SHIM_DIR / "ld").exists()
    gni = (dst / "build" / "config" / "compiler.gni").read_text()
    assert "-fuse-ld=lld" not in gni and "--icf" not in gni
    assert "use_lld = false" in gni
    assert (dst / "build-eld.sh").exists()


def test_link_tool_command_carries_the_shim(tmp_path):
    """The linker choice must land in tool("link"), the one place GN guarantees
    takes effect -- a GN string arg would have its spaces escaped into one token."""
    src = tmp_path / "src"
    (src / "build").mkdir(parents=True)
    (src / ".gn").write_text('buildconfig = "//build/BUILDCONFIG.gn"\n')
    (src / "BUILD.gn").write_text('executable("app") { sources = [ "m.cc" ] }\n')
    (src / "build" / "BUILDCONFIG.gn").write_text('cc = "gcc"\n')
    (src / "build" / "toolchain.gn").write_text(
        'toolchain("gcc") {\n'
        '  tool("cxx") {\n'
        '    command = "g++ {{cflags}} -c {{source}} -o {{output}}"\n'
        '  }\n'
        '  tool("link") {\n'
        '    command = "g++ {{ldflags}} -o {{output}} {{inputs}}"\n'
        '  }\n'
        '  tool("solink") {\n'
        '    command = "g++ -shared {{ldflags}} -o {{output}} {{inputs}}"\n'
        '  }\n'
        '}\n')
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    tc = (dst / "build" / "toolchain.gn").read_text()
    shim = f"-B{dst / SHIM_DIR}/"
    # spliced into both link tools, as separate tokens, right after the driver
    assert f'command = "g++ {shim}' in tc
    assert f'command = "g++ {shim}' in tc.split("solink")[1] or shim in tc.split("solink")[1]
    # the compile tool is untouched
    assert 'command = "g++ {{cflags}} -c {{source}} -o {{output}}"' in tc
    assert tc.count(shim) == 2


def test_checker_understands_a_gn_tree(tmp_path):
    from eldconv.checker import build_commands
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gn").write_text('buildconfig = "//BUILDCONFIG.gn"\n')
    (src / "BUILD.gn").write_text('cc = "gcc"\nldflags = [ "-fuse-ld=lld" ]\n')
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    cmds = build_commands(dst, dst / "out" / "eld", "/x/ld.eld", 2, [])
    assert cmds[-1][:2] == ["ninja", "-C"]
    if shutil.which("gn"):
        assert cmds[0][:2] == ["gn", "gen"]


# ---------------------------------------------------------------- splice targeting

CHROMIUM_SHAPED = '''toolchain("clang") {
  tool("cxx") {
    command = "$cxx -MD -MF $depfile {{defines}} {{cflags}} -c {{source}} -o {{output}}"
  }
  tool("solink") {
    link_command = "$ld -shared $soname_flag {{ldflags}} -o \\"$main_so_file\\" "
    command = "\\"$python_path\\" \\"$link_wrapper\\" --output=\\"$sofile\\" -- $link_command"
  }
  tool("link") {
    link_command = "$ld {{ldflags}} -o \\"$main_outfile\\" "
    command = "\\"$python_path\\" \\"$link_wrapper\\" --output=\\"$outfile\\" -- $link_command"
  }
}
'''


def test_splice_targets_the_driver_not_the_wrapper():
    """Regression: `-B` landed on "$python_path" (the wrapper) instead of $ld."""
    out, _ = rewrite_gn_text(CHROMIUM_SHAPED, "lld", "build/toolchain/gcc_toolchain.gni",
                             select_flags="-B/x/eld-bin/")
    assert out.count("-B/x/eld-bin/") == 2
    assert '"$python_path" -B' not in out and '$python_path\\" -B' not in out
    for line in out.splitlines():
        if "-B/x/eld-bin/" in line:
            assert line.lstrip().startswith("link_command = \"$ld ")


def test_splice_skips_compile_tools():
    """Regression: `-B` was inserted into tool("cxx"), an ordinary compile command."""
    out, _ = rewrite_gn_text(CHROMIUM_SHAPED, "lld", "build/toolchain/gcc_toolchain.gni",
                             select_flags="-B/x/eld-bin/")
    cxx = next(l for l in out.splitlines() if "-MD -MF" in l)
    assert "-B/x/eld-bin/" not in cxx


@pytest.mark.parametrize("path", ["build/toolchain/win/toolchain.gni",
                                  "build/toolchain/apple/toolchain.gni",
                                  "build/toolchain/mac/BUILD.gn",
                                  "build/toolchain/zos/BUILD.gn"])
def test_non_elf_toolchains_are_left_alone(path):
    """MSVC and Apple link drivers do not take -B; eld targets ELF anyway."""
    src = ('toolchain("x") {\n  tool("link") {\n'
           '    command = "$linker_wrapper$link \\"/OUT:$out\\" /nologo"\n  }\n}\n')
    out, diags = rewrite_gn_text(src, "lld", path, select_flags="-B/x/eld-bin/")
    assert "-B/x/eld-bin/" not in out
    assert any("non-ELF" in d.message for d in diags)


def test_msvc_style_link_is_not_spliced_even_outside_a_named_dir():
    src = ('toolchain("x") {\n  tool("link") {\n'
           '    command = "$linker_wrapper$link \\"/OUT:$out\\" /nologo"\n  }\n}\n')
    out, _ = rewrite_gn_text(src, "lld", "build/toolchain/BUILD.gn", select_flags="-B/x/eld-bin/")
    assert "-B/x/eld-bin/" not in out  # $linker_wrapper$link is not a known driver


def test_placeholder_does_not_leak_into_diagnostics():
    src = '      ldflags += [ "--ld-path=" + rebase_path(p, root_build_dir) ]'
    _, diags = rewrite_gn_text(src, "lld")
    assert not any("ELDCONVVALUE" in d.flag or "ELDCONVVALUE" in d.message for d in diags)
