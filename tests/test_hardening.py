"""Production hardening: the eld surface, other-linker -B dirs, direct linker
calls, presets, response files, GCC LTO, the shim's plugin filter, obstacle
scans, safe/atomic output and CLI exit codes."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eldconv import surface
from eldconv.checker import run_check
from eldconv.cli import main
from eldconv.converters.cmake import rewrite_cmake_presets_text
from eldconv.converters.gn import rewrite_ninja_text
from eldconv.converters.make import rewrite_make_text
from eldconv.detect import detect
from eldconv.flags import LinkArg, parse_wl
from eldconv.generator import (REPORT_JSON, SHIM_LD, ConversionError, convert_project,
                               resolve_eld)
from eldconv.mapping import convert_args, drop_severity
from eldconv.obstacles import scan
from eldconv.rewrite import convert_driver_string


def conv(spec, linker="bfd"):
    return convert_args(parse_wl(spec), linker)


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# --------------------------------------------------------------- the surface

def test_bundled_surface_loads(bundled_surface):
    assert "eld 24.0" in bundled_surface.eld_version
    assert set(bundled_surface.targets) >= {"x86_64", "aarch64"}
    assert bundled_surface.accepts("--gc-sections") is True
    assert bundled_surface.accepts("-O") is True      # current eld takes -O<n> and ignores it
    assert bundled_surface.accepts("-z", "now") is True
    assert bundled_surface.accepts("-z", "max-page-size=65536") is True
    assert bundled_surface.accepts("-z", "undefs") is False
    assert bundled_surface.accepts("--never-heard-of-it") is None


def test_distro_ldflags_O1_is_dropped_quietly(bundled_surface):
    """Arch/Gentoo default LDFLAGS carry -Wl,-O1, which eld rejects (and ignores)."""
    out, d = conv("-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now")
    assert [a.parts()[0] for a in out] == ["--sort-common", "--as-needed", "-z", "-z"]
    assert [x.level for x in d] == ["warn"]


def test_q_is_renamed_to_emit_relocs():
    out, d = conv("-Wl,-q")
    assert [a.name for a in out] == ["--emit-relocs"]


def test_unknown_option_eld_rejects_is_dropped_as_error(bundled_surface):
    out, d = conv("-Wl,--dynamic-list-data", "bfd")
    assert out == [] and d[0].level == "error" and "not accepted by eld 24.0" in d[0].message


def test_unknown_harmless_option_eld_rejects_is_a_warning(bundled_surface):
    out, d = conv("-Wl,--no-keep-memory,--stats", "lld")
    assert out == [] and {x.level for x in d} == {"warn"}


def test_unknown_option_eld_accepts_is_kept_silently(bundled_surface):
    out, d = conv("-Wl,--sort-common")
    assert [a.name for a in out] == ["--sort-common"] and d == []


def test_without_surface_unknown_is_kept_with_info():
    out, d = conv("-Wl,--dynamic-list-data")
    assert [a.name for a in out] == ["--dynamic-list-data"] and d[0].level == "info"


def test_z_catch_all_consults_the_surface(bundled_surface):
    out, d = conv("-Wl,-z,interpose")
    assert out == [] and d[0].level == "error"
    out, d = conv("-Wl,-z,mark-plt")
    assert out == [] and d[0].level == "warn"
    out, d = conv("-Wl,-z,brand-new-keyword")      # never probed: table catch-all
    assert out and d[0].level == "info"


def test_tentative_rule_confirmed_by_surface_needs_no_check(bundled_surface):
    out, d = conv("-Wl,--start-lib")
    assert out and d == []


def test_dropped_value_option_takes_its_value_with_it(bundled_surface):
    out, d = conv("-Wl,--oformat,binary")
    assert out == [] and d[0].level == "error"
    out, d = conv("-Wl,--oformat,elf64-x86-64")
    assert out == [] and d[0].level == "warn"
    out, d = conv("-Wl,--filter,libfoo.so")
    assert out == []            # libfoo.so must not survive as a stray input file


def test_require_defined_becomes_undefined_with_warning():
    out, d = conv("-Wl,--require-defined=main")
    assert out[0].parts() == ["--undefined=main"] and d[0].level == "warn"


def test_fix_cortex_a53_843419_is_kept_eld_implements_it(bundled_surface):
    out, d = conv("-Wl,--fix-cortex-a53-843419")
    assert [a.name for a in out] == ["--fix-cortex-a53-843419"] and d == []


def test_drop_severity_classifier():
    assert drop_severity(LinkArg("--warn-constructors")) == "warn"
    assert drop_severity(LinkArg("--thread-count-initial", "2", " ")) == "warn"
    assert drop_severity(LinkArg("--Bsymbolic-non-weak-functions")) == "error"
    assert drop_severity(LinkArg("-z", "nodlopen", " ")) == "error"
    assert drop_severity(LinkArg("-z", "x86-64-v3", " ")) == "warn"


def test_surface_build_with_fake_eld(tmp_path):
    """build() offers every option in a real link and reads the warnings."""
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[0] in ("clang", shutil.which("clang"), "cc"):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x7fELF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, "  --gc-sections  gc\n", "")
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "fake eld 9.9\n", "")
        bad = [a for a in cmd[1:] if "icf" in a or a.startswith("-O")]
        return subprocess.CompletedProcess(cmd, 0, "", f"Warning: Unrecognized option `{bad[0]}'\n" if bad else "")

    s = surface.build("/fake/ld.eld", runner=runner, jobs=4)
    assert s.eld_version == "fake eld 9.9"
    assert s.accepts("--gc-sections") is True
    assert s.accepts("--icf") is False
    assert s.accepts("-O") is False
    assert s.as_dict()["options"]


# --------------------------------------------------- linker selection hiding

def test_mold_libexec_B_dir_is_removed_and_detected(tmp_path):
    out, d = convert_driver_string("-B/usr/libexec/mold -Wl,--gc-sections", "mold")
    assert out == "-Wl,--gc-sections" and d[0].level == "warn"
    out, _ = convert_driver_string("-B /usr/local/libexec/mold/ -O2", "mold")
    assert out == "-O2"
    out, d = convert_driver_string("-B/opt/eld/eld-bin/ -Bsymbolic -B/usr/lib/gcc", "bfd")
    assert out == "-B/opt/eld/eld-bin/ -Bsymbolic -B/usr/lib/gcc" and d == []
    write(tmp_path, "Makefile", "LDFLAGS = -B/usr/libexec/mold\napp:\n\t$(CC) $(LDFLAGS) -o app m.c\n")
    assert detect(tmp_path).linker == "mold"


def test_fuse_ld_with_a_path_is_detected(tmp_path):
    write(tmp_path, "Makefile", "LDFLAGS = -fuse-ld=/usr/bin/ld.lld\n")
    assert detect(tmp_path).linker == "lld"


def test_recipe_calling_a_linker_directly_is_rewritten():
    text = ("x:\n\t@$(Q)ld.gold -r -o a.o b.o && ld -r -o c.o d.o\n"
            "\techo ld is fine; $(CROSS_COMPILE)ld.lld --gc-sections -o y\n"
            "\t$(LD) -r -o z.o q.o\n\t./build-ld.sh ld\n")
    new, d = rewrite_make_text(text, "gold", add_include=False)
    assert new.splitlines()[1] == "\t@$(Q)$(ELD_SHIM_LD) -r -o a.o b.o && $(ELD_SHIM_LD) -r -o c.o d.o"
    assert new.splitlines()[2] == "\techo ld is fine; $(ELD_SHIM_LD) --gc-sections -o y"
    assert "\t$(LD) -r -o z.o q.o" in new and "\t./build-ld.sh ld" in new


def test_recipe_direct_linker_is_detected(tmp_path):
    write(tmp_path, "Makefile", "app.o:\n\tld.gold -r -o app.o a.o\n")
    assert detect(tmp_path).linker == "gold"


def test_ninja_rule_running_a_linker_directly():
    text = "rule ldr\n  command = ld.lld -r --icf=all -o $out $in\nrule cc\n  command = gcc -c $in\n"
    new, d = rewrite_ninja_text(text, "lld", "b.ninja", "-B/s/", "/s/ld")
    assert "  command = /s/ld -r -o $out $in" in new
    assert "  command = gcc -c $in" in new


def test_cmake_presets_linker_type_removed_and_flags_converted(tmp_path):
    text = json.dumps({"version": 6, "configurePresets": [{
        "name": "r", "cacheVariables": {"CMAKE_LINKER_TYPE": "LLD",
                                        "CMAKE_EXE_LINKER_FLAGS": {"type": "STRING", "value": "-Wl,--icf=all -Wl,--gc-sections"}},
        "environment": {"LDFLAGS": "-fuse-ld=lld"}}]})
    new, d = rewrite_cmake_presets_text(text, "lld")
    doc = json.loads(new)
    p = doc["configurePresets"][0]
    assert "CMAKE_LINKER_TYPE" not in p["cacheVariables"]
    assert p["cacheVariables"]["CMAKE_EXE_LINKER_FLAGS"]["value"] == "-Wl,--gc-sections"
    assert p["environment"]["LDFLAGS"] == ""
    write(tmp_path, "CMakeLists.txt", "project(x)\n")
    write(tmp_path, "CMakePresets.json", text)
    assert detect(tmp_path).linker == "lld"


def test_presets_that_are_not_json_fall_back_to_text():
    new, _ = rewrite_cmake_presets_text('{ "x": "-Wl,--icf=all" ', "lld")
    assert "--icf" not in new


# --------------------------------------------------------- response files

def test_in_tree_response_files_are_converted(tmp_path):
    src = tmp_path / "s"
    write(src, "cfg/link.rsp", "--icf=all\n--gc-sections -O1\n")
    write(src, "drv.rsp", "-fuse-ld=lld -Wl,--icf=safe\n")
    write(src, "Makefile", "LDFLAGS = -Wl,@cfg/link.rsp @drv.rsp -Wl,@$(GEN)/x.rsp\n"
                           "app:\n\t$(CC) $(LDFLAGS) -o app m.c\n")
    rep = convert_project(src, tmp_path / "d", eld="/x/ld.eld", source_linker="lld")
    assert (tmp_path / "d/cfg/link.rsp").read_text() == "\n--gc-sections\n"
    assert (tmp_path / "d/drv.rsp").read_text() == "\n"
    by = {d.flag: d for d in rep.diagnostics}
    assert by["@cfg/link.rsp"].level == "info" and by["@drv.rsp"].level == "info"
    assert by["@$(GEN)/x.rsp"].level == "error"       # generated at build time
    assert "cfg/link.rsp" in rep.files_rewritten


def test_response_file_outside_the_tree_is_never_edited(tmp_path):
    outside = write(tmp_path, "outside.rsp", "--icf=all\n")
    src = tmp_path / "s"
    write(src, "Makefile", "LDFLAGS = -Wl,@../outside.rsp\n")
    convert_project(src, tmp_path / "d", eld="/x/ld.eld", source_linker="lld")
    assert outside.read_text() == "--icf=all\n"


# ------------------------------------------------------------------ GCC LTO

def test_gcc_lto_gets_fat_objects_in_cmake(tmp_path):
    src = tmp_path / "s"
    write(src, "CMakeLists.txt", "project(x C)\nset(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)\n"
                                 "add_executable(a a.c)\n")
    rep = convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    assert "set(ELDCONV_GCC_FAT_LTO ON)" in (tmp_path / "d/eld-toolchain.cmake").read_text()
    assert any(d.flag == "-flto" and d.level == "warn" for d in rep.diagnostics)


def test_gcc_lto_gets_fat_objects_in_make(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "CC = gcc\nCFLAGS = -O2 -flto\napp:\n\t$(CC) $(CFLAGS) -o app a.c\n")
    convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    assert "CFLAGS += -ffat-lto-objects" in (tmp_path / "d/eld.mk").read_text()


def test_no_lto_no_fat_objects(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "CC = gcc\napp:\n\t$(CC) -o app a.c\n")
    convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    assert "fat-lto" not in (tmp_path / "d/eld.mk").read_text()


def test_clang_lto_needs_no_fat_objects(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "CC = clang\nCFLAGS = -flto\napp:\n\t$(CC) $(CFLAGS) -o app a.c\n")
    rep = convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    assert "fat-lto" not in (tmp_path / "d/eld.mk").read_text()
    assert not any(d.flag == "-flto" for d in rep.diagnostics)


# --------------------------------------------------------------- the shim

def run_shim(tmp_path, args, env=None):
    eld = tmp_path / "echo-eld"
    eld.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    eld.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(eld)))
    shim.chmod(0o755)
    e = dict(os.environ, **(env or {}))
    e.pop("ELDCONV_ELD", None)
    r = subprocess.run([str(shim), *args], capture_output=True, text=True, env=e, check=True)
    return r.stdout.splitlines()


def test_shim_strips_gcc_plugin_and_its_options(tmp_path):
    out = run_shim(tmp_path, ["-plugin", "/usr/lib/gcc/x/13/liblto_plugin.so",
                              "-plugin-opt=/usr/libexec/gcc/lto-wrapper", "-plugin-opt=-fresolution=x.res",
                              "-o", "app", "a.o"])
    assert out == ["-o", "app", "a.o"]


def test_shim_strips_llvmgold_but_keeps_llvm_plugin_opts(tmp_path):
    out = run_shim(tmp_path, ["-plugin", "/usr/lib/llvm-18/lib/LLVMgold.so", "-plugin-opt=mcpu=x86-64",
                              "-plugin-opt=O2", "-o", "app", "a.o"])
    assert out == ["-plugin-opt=mcpu=x86-64", "-plugin-opt=O2", "-o", "app", "a.o"]


def test_shim_keeps_other_plugins_and_odd_arguments(tmp_path):
    out = run_shim(tmp_path, ["-plugin", "/opt/eld/MyPlugin.so", "a b.o", "", "-plugin"])
    assert out == ["-plugin", "/opt/eld/MyPlugin.so", "a b.o", "", "-plugin"]


def test_shim_filter_can_be_disabled(tmp_path):
    args = ["-plugin", "/x/liblto_plugin.so", "-plugin-opt=-fresolution=r", "a.o"]
    assert run_shim(tmp_path, args, {"ELDCONV_KEEP_GCC_PLUGIN": "1"}) == args


# --------------------------------------------------------------- obstacles

def test_linker_chosen_outside_build_files_is_reported(tmp_path):
    write(tmp_path, "Makefile", "app:\n\t$(CC) -o app m.c\n")
    write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  b:\n    steps:\n      - run: make LDFLAGS=-fuse-ld=mold\n")
    write(tmp_path, "build.sh", "#!/bin/sh\nexport LD=ld.lld\nmake\n")
    write(tmp_path, "docs/notes.txt", "-fuse-ld=lld\n")          # not a config file
    found = {(d.file, d.line) for d in scan(tmp_path)}
    assert found == {(".github/workflows/ci.yml", 4), ("build.sh", 2)}


def test_i386_target_is_flagged(tmp_path):
    write(tmp_path, "Makefile", "LDFLAGS = -m elf_i386\n")
    d = [x for x in scan(tmp_path) if "i386" in x.message]
    assert d and d[0].level == "warn"


# ------------------------------------------------------- safe, atomic output

def test_force_refuses_to_delete_a_directory_eldconv_did_not_write(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    dst = tmp_path / "precious"
    write(dst, "thesis.tex", "years of work")
    with pytest.raises(ConversionError, match="refusing to replace"):
        convert_project(src, dst, eld="/x/ld.eld", force=True)
    assert (dst / "thesis.tex").read_text() == "years of work"


def test_force_replaces_a_previous_conversion(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/x/ld.eld")
    (dst / "stale").write_text("x")
    convert_project(src, dst, eld="/x/ld.eld", force=True)
    assert not (dst / "stale").exists() and (dst / REPORT_JSON).exists()


def test_force_on_an_empty_directory_is_fine(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    (tmp_path / "empty").mkdir()
    convert_project(src, tmp_path / "empty", eld="/x/ld.eld", force=True)
    assert (tmp_path / "empty" / "eld.mk").is_file() and (tmp_path / "empty" / "eldconv-report.json").is_file()


def test_destination_that_contains_the_source_is_rejected(tmp_path):
    src = tmp_path / "proj" / "s"
    write(src, "Makefile", "app:\n")
    with pytest.raises(ConversionError, match="contains the source"):
        convert_project(src, tmp_path / "proj", eld="/x/ld.eld", force=True)
    assert (src / "Makefile").exists()


def test_failed_conversion_leaves_nothing_behind(tmp_path, monkeypatch):
    import eldconv.generator as g
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(g, "scan_obstacles", boom)
    with pytest.raises(RuntimeError):
        convert_project(src, tmp_path / "d", eld="/x/ld.eld")
    assert not (tmp_path / "d").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".d.eldconv-")]


def test_source_must_be_a_directory(tmp_path):
    with pytest.raises(ConversionError):
        convert_project(tmp_path / "missing", tmp_path / "d")


# -------------------------------------------------------------- eld path

def test_resolve_eld_makes_relative_paths_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exe = write(tmp_path, "bin/ld.eld", "#!/bin/sh\n")
    exe.chmod(0o755)
    path, warn = resolve_eld("bin/ld.eld")
    assert path == str(exe) and warn is None
    path, warn = resolve_eld("./nope/ld.eld")
    assert path == str(tmp_path / "nope/ld.eld") and "not an executable" in warn


def test_resolve_eld_bare_name_not_on_path_warns(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    path, warn = resolve_eld("ld.eld")
    assert path == "ld.eld" and "PATH" in warn


@pytest.mark.parametrize("bad", ['/a"b/ld.eld', "/a$HOME/ld.eld", "/a`x`/ld", "/my tools/ld.eld", ""])
def test_resolve_eld_rejects_unsafe_paths(bad):
    with pytest.raises(ConversionError):
        resolve_eld(bad)


def test_eld_version_mismatch_is_reported(tmp_path, bundled_surface):
    eld = write(tmp_path, "other/ld.eld", "#!/bin/sh\necho 'GNU ld compatible linker - eld 99.0'\n")
    eld.chmod(0o755)
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    rep = convert_project(src, tmp_path / "d", eld=str(eld))
    assert any(d.flag == "eld version" and "eld 99.0" in d.message for d in rep.diagnostics)


# ------------------------------------------------------------------ CLI

def test_cli_strict_fails_on_errors(tmp_path, capsys):
    src = tmp_path / "s"
    write(src, "Makefile", "LDFLAGS = -fuse-ld=lld -Wl,--icf=all\n")
    assert main(["convert", str(src), str(tmp_path / "d"), "--eld", "/x/ld.eld", "-q"]) == 0
    assert main(["convert", str(src), str(tmp_path / "d2"), "--eld", "/x/ld.eld", "-q", "--strict"]) == 1


def test_cli_reports_bad_input_with_exit_2(tmp_path, capsys):
    assert main(["convert", str(tmp_path / "missing"), str(tmp_path / "d")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_eld_surface_none_and_file(tmp_path, capsys):
    s = surface.Surface("x", ["x86_64"], {"--gc-sections": []}, {})
    f = tmp_path / "s.json"
    f.write_text(json.dumps(s.as_dict()))
    main(["flags", "-Wl,--gc-sections", "--eld-surface", str(f)])
    assert capsys.readouterr().out.strip() == ""            # this "eld" rejects it
    main(["flags", "-Wl,--gc-sections", "--eld-surface", "none"])
    assert capsys.readouterr().out.strip() == "-Wl,--gc-sections"
    assert main(["flags", "-Wl,-x", "--eld-surface", str(tmp_path / "nope.json")]) == 2


# --------------------------------------------------------------- checker

def test_missing_build_tool_is_a_problem_not_a_traceback(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/bin/true")

    def runner(cmd, **kw):
        if cmd[0] == "make":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    res = run_check(dst, eld="/bin/true", runner=runner, probe=False)
    assert not res.ok and any("build tool not found: make" in p for p in res.problems)


def test_build_timeout_is_a_problem(tmp_path):
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/bin/true")

    def runner(cmd, **kw):
        if cmd[0] == "make" and "clean" not in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"), output="partial", stderr="")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    res = run_check(dst, eld="/bin/true", runner=runner, probe=False, timeout=5)
    assert not res.ok and any("timed out after 5s" in p for p in res.problems)


def test_eld_mk_shim_dir_is_absolute(tmp_path):
    """A recursive build (`make -C sub`) changes directory; a relative shim dir
    exported through COMPILER_PATH/-B would then point somewhere else (found
    on zstd, whose sub-make links from programs/)."""
    import shutil
    import subprocess
    if not shutil.which("make"):
        pytest.skip("make not installed")
    from eldconv.generator import convert_project
    src = tmp_path / "s"
    (src / "sub").mkdir(parents=True)
    (src / "Makefile").write_text("all:\n\t$(MAKE) -C sub\n")
    (src / "sub" / "Makefile").write_text("all:\n\t@echo CP=$$COMPILER_PATH\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/bin/true")
    out = subprocess.run(["make", "-s", "-C", str(dst)], capture_output=True, text=True).stdout
    cp = [l for l in out.splitlines() if l.startswith("CP=")][0][3:]
    assert cp.split(":")[0] == str(dst / "eld-bin")


def test_devnull_and_failed_oneshot_links_are_probes(tmp_path):
    from eldconv.checker import LinkInvocation
    assert LinkInvocation(str(tmp_path), ["-o", "/dev/null", "x.o"]).is_trycompile()
    # driver temp object, output absent -> a failed feature probe, not a failed build
    inv = LinkInvocation(str(tmp_path), ["-o", "have_zlib", "/usr/lib/Scrt1.o", "/usr/lib/crti.o",
                                         "/usr/lib/gcc/crtbeginS.o", "/tmp/ccAbC123x.o", "-lgcc"])
    assert inv.is_trycompile()
    # a named object that produced no ELF is a real failure
    assert not LinkInvocation(str(tmp_path), ["main.o", "-o", "tool"]).is_trycompile()


def test_shim_filters_z_hints_eld_rejects(tmp_path):
    import os, subprocess
    from eldconv.generator import SHIM_LD
    fake = tmp_path / "eld"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "[$a]"; done\n')
    fake.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(fake)))
    shim.chmod(0o755)

    def run(*args):
        return subprocess.run([str(shim), *args], capture_output=True, text=True).stdout.splitlines()
    assert run("-z", "noexecstack", "-z", "cet-report=error", "-z", "ibt", "-zshstk", "-o", "a", "x.o") \
        == ["[-z]", "[noexecstack]", "[-o]", "[a]", "[x.o]"]
    # empty and space-containing arguments survive; a trailing -z is kept untouched
    assert run("", "a b", "-z") == ["[]", "[a b]", "[-z]"]
    assert run("-z", "ibt", "") == ["[]"]
    env = dict(os.environ, ELDCONV_KEEP_Z_HINTS="1")
    out = subprocess.run([str(shim), "-z", "ibt"], capture_output=True, text=True, env=env).stdout.splitlines()
    assert out == ["[-z]", "[ibt]"]


def test_make_silent_prefix_is_not_a_response_file():
    from eldconv.rewrite import Rewriter
    rw = Rewriter("bfd")
    rw.rewrite_line('\t@echo "LDFLAGS="$(LDFLAGS) -Wl,--gc-sections')
    assert not rw.response_files
    rw.rewrite_line('CC -Wl,@real.rsp')
    assert "real.rsp" in rw.response_files


def test_force_exe_suffix_is_pe_only_and_harmless():
    from eldconv.flags import parse_wl
    from eldconv.mapping import convert_args
    out, d = convert_args(parse_wl("-Wl,--force-exe-suffix"), "bfd")
    assert not [x for x in d if x.level == "error"]


def test_check_pre_build_commands_run_first(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n")
    (tmp_path / "eld.mk").write_text("# eld\n")
    res = run_check(tmp_path, eld="/bin/true", dry_run=True, pre_build=["./configure --prefix=/x"])
    assert res.commands[0] == ["sh", "-c", "./configure --prefix=/x"]
    assert res.commands[1][0] == "make"


def test_makefile_in_at_root_is_an_entry_point():
    from eldconv.converters.make import needs_include
    assert needs_include("all:\n", "all:\n", 0, "Makefile.in")
    assert not needs_include("all:\n", "all:\n", 1, "Makefile.in")


def test_make_automatic_vars_and_autoconf_placeholders_are_not_response_files():
    from eldconv.rewrite import Rewriter
    rw = Rewriter("bfd")
    for line in ('\t$(CC) -Wl,--as-needed $(LDLIBS_$(@F)) -o $@',
                 '\t$(CC) -Wl,--as-needed $(@D)/x.o',
                 'LDFLAGS = -Wl,--as-needed -version-info @LIBUSBIP_VERSION@'):
        rw.rewrite_line(line)
    assert not rw.response_files


def test_dangling_wl_T_in_the_source_is_not_reported_as_lost():
    out, d = convert_args(parse_wl("-Wl,-T"), "bfd")
    assert not [x for x in d if "none survived" in x.message]


def test_shim_help_mentions_supported_targets_for_libtool(tmp_path):
    import subprocess
    from eldconv.generator import SHIM_LD
    fake = tmp_path / "eld"
    fake.write_text('#!/bin/sh\necho "eld help"\n')
    fake.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(fake)))
    shim.chmod(0o755)
    out = subprocess.run([str(shim), "-m", "elf_x86_64", "--help"], capture_output=True, text=True).stdout
    assert "eld help" in out and ": supported targets: " in out and "elf" in out
    assert ": supported targets" not in subprocess.run([str(shim), "-v"], capture_output=True, text=True).stdout


def _tiny_elf(tmp_path, with_reloc):
    """A minimal ELF64 executable image with .dynsym [null, UND GLOBAL OBJECT 'stdout']
    and a .rela.dyn that does (or does not) reference it."""
    import struct
    dynstr = b"\0stdout\0"
    dynsym = bytes(24) + struct.pack("<IBBHQQ", 1, (1 << 4) | 1, 0, 0, 0, 0)
    rela = struct.pack("<QQq", 0x1000, (1 << 32) | 6, 0) if with_reloc else b""
    shstr = b"\0.dynsym\0.dynstr\0.rela.dyn\0.shstrtab\0"

    def off(n):
        return shstr.index(n.encode())
    blobs = [dynsym, dynstr, rela, shstr]
    pos = 64
    offs = []
    for b in blobs:
        offs.append(pos)
        pos += len(b)
    shoff = (pos + 7) & ~7
    # section headers: null, dynsym(11, link=2), dynstr(3), rela(4, link=1), shstrtab(3)
    def sh(name, typ, o, size, link=0, entsize=0):
        return struct.pack("<IIQQQQIIQQ", name, typ, 0, 0, o, size, link, 0, 8, entsize)
    shdrs = (bytes(64) + sh(off(".dynsym"), 11, offs[0], len(dynsym), 2, 24)
             + sh(off(".dynstr"), 3, offs[1], len(dynstr))
             + sh(off(".rela.dyn"), 4, offs[2], len(rela), 1, 24)
             + sh(off(".shstrtab"), 3, offs[3], len(shstr)))
    ehdr = (b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 5, 4))
    data = ehdr + b"".join(blobs)
    data += bytes(shoff - len(data)) + shdrs
    p = tmp_path / ("ok" if with_reloc else "bad")
    p.write_bytes(data)
    return p


def test_unresolved_data_symbol_detected(tmp_path):
    from eldconv.checker import unresolved_data_symbols
    assert unresolved_data_symbols(_tiny_elf(tmp_path, with_reloc=False)) == ["stdout"]
    assert unresolved_data_symbols(_tiny_elf(tmp_path, with_reloc=True)) == []


def test_unresolved_data_symbol_ignores_non_elf_and_good_binaries(tmp_path):
    import shutil
    import subprocess
    from eldconv.checker import unresolved_data_symbols
    (tmp_path / "t.txt").write_text("not elf")
    assert unresolved_data_symbols(tmp_path / "t.txt") == []
    if shutil.which("cc"):
        (tmp_path / "s.c").write_text('#include <stdio.h>\nint main(void){fputs("x",stdout);return 0;}\n')
        subprocess.run(["cc", "-o", str(tmp_path / "s"), str(tmp_path / "s.c")], check=True)
        assert unresolved_data_symbols(tmp_path / "s") == []


def test_kbuild_tree_does_not_export_compiler_path(tmp_path):
    """Kbuild links host tools with HOSTCC; exporting COMPILER_PATH would send them through eld."""
    from eldconv.generator import convert_project
    for name, kbuild in (("k", True), ("p", False)):
        src = tmp_path / name
        (src / "scripts").mkdir(parents=True)
        (src / "Makefile").write_text("LDFLAGS += -fuse-ld=gold\nall:\n\t$(CC) -o a a.c $(LDFLAGS)\n")
        if kbuild:
            (src / "scripts" / "Kbuild.include").write_text("# kbuild\n")
        dst = tmp_path / (name + "-out")
        convert_project(src, dst, eld="/bin/true")
        mk = (dst / "eld.mk").read_text()
        assert ("export COMPILER_PATH" in mk) == (not kbuild)


def test_ld_assigned_with_colon_equals_is_usable_before_eld_mk(tmp_path):
    """`LD := ld` followed by a use of $(LD) must already name the shim (eld.mk comes last)."""
    import shutil
    import subprocess
    if not shutil.which("make"):
        pytest.skip("make not installed")
    from eldconv.generator import convert_project
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text("LD := ld\nWHERE := $(LD)\nall:\n\t@echo WHERE=$(WHERE)\n")
    dst = tmp_path / "d"
    convert_project(src, dst, eld="/bin/true")
    out = subprocess.run(["make", "-s", "-C", str(dst)], capture_output=True, text=True).stdout
    assert out.strip() == f"WHERE={dst}/eld-bin/ld"


# ---------------------------------------------------------------- review round

def test_cmake_select_flags_are_one_quoted_string():
    """`-fno-use-linker-plugin` outside the quotes made the value a ';' list, which Ninja put
    on the link line verbatim."""
    from eldconv.generator import TOOLCHAIN_CMAKE
    line = [ln for ln in TOOLCHAIN_CMAKE.splitlines() if ln.startswith("set(ELD_SELECT_FLAGS")][0]
    assert line.count('"') == 2 and line.endswith('")') and '@NOPLUGIN@"' in line


def test_check_never_wipes_a_directory_it_does_not_own(tmp_path):
    from eldconv.checker import _safe_to_wipe
    dst = tmp_path / "proj" / "out"
    (dst / "build-eld").mkdir(parents=True)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "thesis.tex").write_text("x")
    assert _safe_to_wipe(dst / "build-eld", dst)
    assert not _safe_to_wipe(precious, dst)              # unrelated directory
    assert not _safe_to_wipe(dst / "..", dst)            # ancestor
    assert not _safe_to_wipe(dst, dst)
    assert not _safe_to_wipe(Path("/"), dst)
    (precious / "CMakeCache.txt").write_text("")
    assert _safe_to_wipe(precious, dst)                  # recognisably a CMake build directory


def test_check_build_dir_parent_is_not_deleted(tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CMakeLists.txt").write_text("project(x C)\n")
    (proj / "eld-toolchain.cmake").write_text("")
    run_check(proj, eld="/bin/true", build_dir=Path(".."), probe=False, timeout=20)
    assert (tmp_path / "keep.txt").exists() and proj.exists()


@pytest.mark.parametrize("spelling", ["--relocatable", "-relocatable"])
def test_relocatable_is_renamed_not_dropped(spelling):
    from eldconv.flags import parse_wl
    from eldconv.mapping import convert_args
    out, d = convert_args(parse_wl(f"-Wl,{spelling}"), "bfd")
    assert [a.parts() for a in out] == [["-r"]]       # dropping it turns a partial link into a full one


def _symbol_elf(tmp_path, bind, typ, with_reloc=False, name="environ"):
    import struct
    dynstr = b"\0" + name.encode() + b"\0"
    dynsym = bytes(24) + struct.pack("<IBBHQQ", 1, (bind << 4) | typ, 0, 0, 0, 0)
    rela = struct.pack("<QQq", 0x1000, (1 << 32) | 6, 0) if with_reloc else b""
    shstr = b"\0.dynsym\0.dynstr\0.rela.dyn\0.shstrtab\0"
    blobs, pos, offs = [dynsym, dynstr, rela, shstr], 64, []
    for b in blobs:
        offs.append(pos)
        pos += len(b)
    shoff = (pos + 7) & ~7

    def sh(n, t, o, size, link=0, ent=0):
        return struct.pack("<IIQQQQIIQQ", shstr.index(n.encode()) if n else 0, t, 0, 0, o, size, link, 0, 8, ent)
    shdrs = (bytes(64) + sh(".dynsym", 11, offs[0], len(dynsym), 2, 24) + sh(".dynstr", 3, offs[1], len(dynstr))
             + sh(".rela.dyn", 4, offs[2], len(rela), 1, 24) + sh(".shstrtab", 3, offs[3], len(shstr)))
    ehdr = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 5, 4)
    data = ehdr + b"".join(blobs)
    data += bytes(shoff - len(data)) + shdrs
    p = tmp_path / f"e{bind}{typ}{int(with_reloc)}"
    p.write_bytes(data)
    return p


def test_weak_undefined_data_symbols_without_relocation_are_reported(tmp_path):
    from eldconv.checker import unresolved_data_symbols
    assert unresolved_data_symbols(_symbol_elf(tmp_path, bind=2, typ=1)) == ["environ"]   # WEAK OBJECT
    assert unresolved_data_symbols(_symbol_elf(tmp_path, bind=2, typ=1, with_reloc=True)) == []
    assert unresolved_data_symbols(_symbol_elf(tmp_path, bind=2, typ=0)) == []              # weak NOTYPE: fine
    assert unresolved_data_symbols(_symbol_elf(tmp_path, bind=1, typ=2)) == []              # FUNC: fine


def test_malformed_elf_never_raises(tmp_path):
    import random
    from eldconv.checker import elf_notes_and_comment, elf_type, is_elf, unresolved_data_symbols
    base = _symbol_elf(tmp_path, bind=1, typ=1).read_bytes()
    rng = random.Random(1234)
    f = tmp_path / "fuzz"
    for _ in range(1500):
        b = bytearray(base[:rng.choice([6, 20, 64, 120, len(base)])])
        for _ in range(rng.randint(0, 6)):
            if b:
                b[rng.randrange(len(b))] = rng.randrange(256)
        if len(b) > 0x30 and rng.random() < .2:
            b[0x28:0x30] = b"\xff" * 8
        f.write_bytes(bytes(b))
        is_elf(f), elf_type(f), elf_notes_and_comment(f), unresolved_data_symbols(f)


def test_shim_log_records_survive_parallel_links(tmp_path):
    import subprocess
    from eldconv.generator import SHIM_LD
    fake = tmp_path / "eld"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(fake)))
    shim.chmod(0o755)
    log = tmp_path / "log"
    env = dict(os.environ, ELDCONV_LINK_LOG=str(log))
    procs = [subprocess.Popen([str(shim), "-o", f"out{i}", f"a{i}.o", "-lm"], env=env) for i in range(150)]
    for p in procs:
        p.wait()
    lines = log.read_text().splitlines()
    assert len(lines) == 150
    assert all(ln.count("\x1f") == 5 for ln in lines)       # cwd + 4 args, each terminated


def test_autoconf_placeholders_and_semicolon_prefix_are_not_response_files():
    from eldconv.rewrite import Rewriter
    for line in ("LDFLAGS = -Wl,--version-script,@srcdir@/foo.map",
                 "LDFLAGS = -Wl,-rpath,@libdir@/foo",
                 "all: ; @echo $(LD) -Wl,--gc-sections"):
        rw = Rewriter("bfd")
        rw.rewrite_line(line)
        assert not rw.response_files, line


def test_cmake_args_with_spaces_and_semicolons_are_quoted_in_build_script(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "CMakeLists.txt").write_text("project(h C)\nadd_executable(h h.c)\n")
    (src / "h.c").write_text("int main(void){return 0;}\n")
    dst = tmp_path / "d"
    main(["convert", str(src), str(dst), "--eld", "/bin/true", "-q",
          "--cmake-arg=-DCMAKE_C_FLAGS=-O2 -g", "--cmake-arg=-DX=a;b"])
    script = (dst / "build-eld.sh").read_text()
    assert "'-DCMAKE_C_FLAGS=-O2 -g'" in script and "'-DX=a;b'" in script


def test_destination_with_whitespace_or_symlinked_parent_is_rejected(tmp_path):
    from eldconv.generator import ConversionError, convert_project
    src = tmp_path / "s"
    write(src, "Makefile", "app:\n")
    with pytest.raises(ConversionError):
        convert_project(src, tmp_path / "has space" / "out", eld="/x/ld.eld")
    link = tmp_path / "lnk"
    link.symlink_to(src)
    with pytest.raises(ConversionError):
        convert_project(src, link / "out", eld="/x/ld.eld")      # would land inside the source
    assert not (src / "out").exists()


def test_check_with_missing_eld_is_a_usage_error(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "eld.mk").write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["check", str(tmp_path), "--eld", str(tmp_path / "nope")]) == 2


def test_help_flag_hidden_inside_another_argument_is_not_special(tmp_path):
    import subprocess
    from eldconv.generator import SHIM_LD
    fake = tmp_path / "eld"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    shim = tmp_path / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(fake)))
    shim.chmod(0o755)
    out = subprocess.run([str(shim), "-o", "x --help y"], capture_output=True, text=True).stdout
    assert "supported targets" not in out


def test_mktemp_probe_links_in_tmp_are_probes(tmp_path):
    from eldconv.checker import LinkInvocation
    assert LinkInvocation("/src", ["-o", "/tmp/tmp.AbC123", "x.o"]).is_trycompile()
    assert not LinkInvocation("/src", ["-o", "/src/out/app", "x.o"]).is_trycompile()


def test_unresolved_data_symbols_fail_check_unless_allowed(tmp_path):
    """A linked ELF whose `stdout` has no relocation is a failed check by default."""
    import shutil
    if not shutil.which("make"):
        pytest.skip("make not installed")
    bad = _symbol_elf(tmp_path, bind=1, typ=1, name="stdout")
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text(f"app:\n\tcp {bad} app\n")
    (src / "eld.mk").write_text("")
    shim = src / "eld-bin"
    shim.mkdir()
    (shim / "ld").write_text("#!/bin/sh\nprintf '%s\\037-o\\037app\\037x.o\\037\\n' \"$PWD\" >> \"$ELDCONV_LINK_LOG\"\n")
    (shim / "ld").chmod(0o755)
    (src / "Makefile").write_text(f".PHONY: app\napp:\n\tcp {bad} app\n\t{shim}/ld -o app x.o\n")
    res = run_check(src, eld="/bin/true", probe=False, clean=False, timeout=60)
    assert any("undefined data symbol" in p for p in res.problems)
    res = run_check(src, eld="/bin/true", probe=False, clean=False, timeout=60, allow_unresolved_data=True)
    assert not any("undefined data symbol" in p for p in res.problems)
    assert any("undefined data symbol" in w for w in res.warnings)
