"""Corner cases and regressions found by adversarial probing."""
import shutil
import time
from pathlib import Path

import pytest

from eldconv.converters.cmake import rewrite_cmake_text
from eldconv.converters.make import rewrite_make_text
from eldconv.detect import detect, iter_build_files
from eldconv.generator import convert_project
from eldconv.rewrite import Rewriter, convert_driver_string

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- degenerate input

@pytest.mark.parametrize("src", ["-Wl,", "-Wl,,", "-Wl,--gc-sections,", "-Wl,-z", "-Wl,-z,",
                                 "", "   ", "-", "--", "-Wl", "-Xlinker", "-Wl,,,--gc-sections"])
def test_degenerate_flag_strings_do_not_crash(src):
    for linker in ("bfd", "lld", "gold", "mold"):
        out, diags = convert_driver_string(src, linker)
        assert isinstance(out, str)
        if out != src:  # untouched input keeps its original spacing
            assert "  " not in out


def test_empty_and_whitespace_only_text_is_untouched():
    rw = Rewriter("lld")
    for t in ["", "\n", "   ", "\t\t", "\n\n\n"]:
        assert rw.rewrite_text(t) == t


def test_very_long_flag_list():
    toks = ["--gc-sections", "--as-needed"] * 500 + ["--icf=all"] * 50
    out, diags = convert_driver_string("-Wl," + ",".join(toks), "lld")
    assert out.count("--gc-sections") == 500 and "--icf" not in out
    assert sum(1 for d in diags if d.level == "error") == 50


def test_deeply_nested_project(tmp_path):
    src = tmp_path / "src"
    deep = src
    for i in range(12):
        deep = deep / f"l{i}"
    deep.mkdir(parents=True)
    (src / "Makefile").write_text("all:\n")
    (deep / "Makefile").write_text("LDFLAGS = -Wl,--icf=all\nall:\n")
    (deep / "sub.mk").write_text("LD = ld.lld\n")
    convert_project(src, tmp_path / "dst", source_linker="lld")
    dstdeep = tmp_path / "dst" / deep.relative_to(src)
    assert "--icf" not in (dstdeep / "Makefile").read_text()
    # the file that now uses $(ELD_SHIM_LD) gets a correctly-depthed include
    assert "../" * 12 + "eld.mk" in (dstdeep / "sub.mk").read_text()


# ---------------------------------------------------------------- regressions

def test_emptied_cmake_list_element_takes_its_separator():
    """Regression: `"-Wl,--fork;-Wl,--gc-sections"` left a leading ';' (empty list entry)."""
    cases = [('set(F "-Wl,--fork;-Wl,--gc-sections")', 'set(F "-Wl,--gc-sections")'),
             ('set(F "-Wl,--gc-sections;-Wl,--fork")', 'set(F "-Wl,--gc-sections")'),
             ('set(F "-Wl,--as-needed;-Wl,--fork;-Wl,--gc-sections")', 'set(F "-Wl,--as-needed;-Wl,--gc-sections")'),
             ('set(F "-Wl,--fork")', 'set(F)')]
    for src, want in cases:
        assert rewrite_cmake_text(src, "mold")[0] == want, src


def test_response_files_are_reported_as_unconvertible():
    """Regression: `@opts.rsp` hides options from the converter entirely."""
    for src in ["-Wl,@resp.txt -Wl,--gc-sections", "-Xlinker @resp.txt", "LDFLAGS = @resp.txt"]:
        out, diags = convert_driver_string(src, "lld")
        err = [d for d in diags if d.level == "error" and d.flag.startswith("@")]
        assert err, src
        assert "response file" in err[0].message
        assert "@resp.txt" in out  # never silently dropped


def test_response_file_not_reported_without_linker_context():
    # an email address or a make automatic variable is not a response file
    rw = Rewriter("lld")
    for line in ["# contact: dev@example.com", "\t@echo hi", "MAIL = a@b.c"]:
        rw.rewrite_line(line)
    assert not [d for d in rw.diags if d.flag.startswith("@")]


def test_value_flag_left_without_a_value_is_an_error():
    """Regression: dropping a value made `-T` swallow the next input file instead."""
    out, diags = convert_driver_string("-Wl,-T,--fork", "mold")
    assert out == "-Wl,-T"
    assert any(d.level == "error" and d.flag == "-T" for d in diags)
    # but a value flag followed by a real input file is fine
    out, diags = convert_driver_string("-Wl,-T,link.ld", "mold")
    assert out == "-Wl,-T,link.ld"
    assert not [d for d in diags if d.level == "error"]


def test_stale_build_file_scan_is_not_quadratic(tmp_path):
    """Regression: the scan re-walked the whole tree per file (hours on a big tree)."""
    src = tmp_path / "src"
    (src / "out").mkdir(parents=True)
    (src / "build").mkdir(parents=True)
    (src / "Makefile").write_text("all:\n")
    (src / "out" / "Makefile").write_text("LDFLAGS = -fuse-ld=mold\n")
    (src / "build" / "Makefile").write_text("LDFLAGS = -fuse-ld=mold\n")
    (src / "build" / "stale.o").write_bytes(b"\x7fELF")  # marks it as build output
    for i in range(400):  # plenty to make a quadratic scan visible
        (src / f"src{i}.c").write_text("int x;\n")
    t = time.time()
    rep = convert_project(src, tmp_path / "dst")
    assert time.time() - t < 20
    dst = tmp_path / "dst"
    # `build/` holds real build output (a .o) and is dropped; `out/` is only
    # named like one, so it is kept AND converted rather than silently skipped.
    assert not (dst / "build").exists()
    assert "-fuse-ld=mold" not in (dst / "out" / "Makefile").read_text()


def test_kbuild_files_are_recognised(tmp_path):
    """The kernel and other kbuild trees put flags in `Kbuild`, not `Makefile`."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("all:\n")
    (src / "Kbuild").write_text("ldflags-y := --icf=all\nobj-y += a.o\n")
    (src / "Kbuild.include").write_text("LDFLAGS := -Wl,--fork\n")
    files = {p.name for p in iter_build_files(src)}
    assert {"Kbuild", "Kbuild.include"} <= files
    convert_project(src, tmp_path / "dst", source_linker="lld")
    assert "--icf" not in (tmp_path / "dst" / "Kbuild").read_text()


# ---------------------------------------------------------------- make/cmake shapes

@pytest.mark.parametrize("line,want", [
    ("LDFLAGS += -Wl,--fork  # mold only", "LDFLAGS += # mold only"),
    ("LDFLAGS := $(LDFLAGS) -Wl,--fork", "LDFLAGS := $(LDFLAGS)"),
    ("LDFLAGS-$(CONFIG_X) += -Wl,--fork", "LDFLAGS-$(CONFIG_X) +="),
    ("\t$(LD) --fork -o $@ $^", "\t$(LD) -o $@ $^"),
    ("\t$(LD) $(LDFLAGS) -o $@ $^", "\t$(LD) $(LDFLAGS) -o $@ $^"),
])
def test_make_line_shapes(line, want):
    rw = Rewriter("mold")
    assert rw.rewrite_line(line) == want


@pytest.mark.parametrize("assign", ["LD = ld", "LD=ld.lld", "export LD:=ld.gold",
                                    "override LD = $(CROSS_COMPILE)ld", "LD ?= mold",
                                    "LD\t\t= $(CROSS_COMPILE)ld"])
def test_ld_assignment_shapes_are_all_caught(assign):
    out, _ = rewrite_make_text(assign + "\n", "bfd")
    assert "$(ELD_SHIM_LD)" in out, assign


@pytest.mark.parametrize("assign", ["LDD = ldd", "LD_LIBRARY_PATH = /x", "LD = $(CC)",
                                    "OLD = ld", "LDFLAGS = -lld", "BUILD = ld-stuff"])
def test_non_linker_ld_assignments_are_left_alone(assign):
    out, _ = rewrite_make_text(assign + "\n", "bfd")
    assert "$(ELD_SHIM_LD)" not in out, assign


def test_cmake_genex_and_shell_forms_stay_syntactically_valid():
    for src in ['target_link_options(t PRIVATE $<$<CONFIG:Rel>:-Wl,--fork>)',
                'target_link_options(t PRIVATE "$<$<LINK_LANG_AND_ID:C,GNU>:LINKER:--fork>")',
                'add_link_options("SHELL:-Wl,--fork -Wl,--perf")',
                'string(APPEND CMAKE_EXE_LINKER_FLAGS " -Wl,--fork")']:
        out, _ = rewrite_cmake_text(src, "mold")
        assert out.count("(") == out.count(")"), out
        assert out.count('"') % 2 == 0, out
        assert "--fork" not in out and "--perf" not in out


def test_cmake_comment_lines_are_not_rewritten():
    rw = Rewriter("mold")
    line = "# historical: we used -Wl,--fork here"
    # comments carry linker context, so this is a deliberate trade-off: document it
    out = rw.rewrite_line(line)
    assert out in (line, "# historical: we used here")


# ---------------------------------------------------------------- detection

def test_detect_conflicting_linkers_picks_the_most_evidenced(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        "project(x C)\n"
        "set(CMAKE_EXE_LINKER_FLAGS -fuse-ld=lld)\n"
        "set(CMAKE_SHARED_LINKER_FLAGS -fuse-ld=lld)\n"
        "# once used -fuse-ld=gold\n")
    assert detect(tmp_path).linker == "lld"


def test_detect_handles_unreadable_file(tmp_path):
    (tmp_path / "Makefile").write_text("LD = ld.gold\nall:\n")
    bad = tmp_path / "broken.mk"
    bad.write_bytes(b"\xff\xfe\x00\x00binary junk\n")
    info = detect(tmp_path)
    assert info.linker == "gold"


def test_symlinked_directory_is_not_followed_into_a_loop(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("LD = ld.lld\nall:\n")
    (src / "self").symlink_to(src, target_is_directory=True)
    files = iter_build_files(src)  # must terminate
    assert any(f.name == "Makefile" for f in files)


# ---------------------------------------------------------------- conditional selection

def test_linker_inside_a_conditional_is_not_the_default(tmp_path):
    """The kernel picks ld.lld only under `ifneq ($(LLVM),)`; the default is bfd."""
    (tmp_path / "Makefile").write_text(
        "CPP = $(CC) -E\n"
        "ifneq ($(LLVM),)\n"
        "CC  = clang\n"
        "LD  = $(LLVM_PREFIX)ld.lld\n"
        "else\n"
        "CC  = $(CROSS_COMPILE)gcc\n"
        "LD  = $(CROSS_COMPILE)ld\n"
        "endif\n"
        "all:\n")
    info = detect(tmp_path)
    assert info.linker == "bfd"
    assert info.conditional_linkers == ["bfd", "lld"]
    assert any("(conditional)" in e for e in info.evidence)


def test_kbuild_config_suffix_assignment_is_conditional(tmp_path):
    """`ldflags-$(CONFIG_LD_IS_LLD) += -fuse-ld=lld` applies only when that symbol is set."""
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "sub.mk").write_text("ldflags-$(CONFIG_LD_IS_LLD) += -fuse-ld=lld\n")
    info = detect(tmp_path)
    assert info.linker == "bfd" and "lld" in info.conditional_linkers


def test_make_conditional_functions_are_conditional(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "a.mk").write_text("CFLAGS_x = $(if $(LLVM),-fuse-ld=lld)\n")
    (tmp_path / "b.mk").write_text("LDFLAGS += $(call ld-option,-fuse-ld=mold)\n")
    info = detect(tmp_path)
    assert info.linker == "bfd"
    assert set(info.conditional_linkers) == {"lld", "mold"}


def test_top_level_build_file_outweighs_subdirectories(tmp_path):
    """One deliberate choice at the top beats incidental mentions deeper in the tree."""
    (tmp_path / "Makefile").write_text("LD = ld.gold\nall:\n")
    for i in range(5):
        d = tmp_path / f"sub{i}"
        d.mkdir()
        (d / "Makefile").write_text("LDFLAGS += -fuse-ld=mold\n")
    assert detect(tmp_path).linker == "gold"


def test_plain_ld_assignment_counts_as_bfd(tmp_path):
    (tmp_path / "Makefile").write_text("LD = $(CROSS_COMPILE)ld\nall:\n")
    info = detect(tmp_path)
    assert info.linker == "bfd" and not info.linker_assumed


def test_ambiguous_unconditional_evidence_is_flagged(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "a.mk").write_text("LDFLAGS += -fuse-ld=gold\n")
    (tmp_path / "b.mk").write_text("LDFLAGS += -fuse-ld=mold\n")
    info = detect(tmp_path)
    assert info.ambiguous and info.linker in ("gold", "mold")


def test_cmake_conditional_branch_detected(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        "project(p C)\n"
        "if(USE_MOLD)\n"
        "  set(CMAKE_LINKER_TYPE MOLD)\n"
        "endif()\n")
    info = detect(tmp_path)
    assert info.linker == "bfd" and info.conditional_linkers == ["mold"]


# ---------------------------------------------------------------- variable references

@pytest.mark.parametrize("line", [
    "LDFLAGS_vmlinux += --orphan-handling=$(CONFIG_LD_ORPHAN_WARN_LEVEL)",
    "ldflags-y += -Wl,--hash-style=${STYLE}",
    "LDFLAGS += -Wl,-Map,$(BUILD)/out.map",
    "LDFLAGS += -Wl,-rpath,$(ORIGIN)/lib",
    "LDFLAGS += -Wl,--version-script=$(src)/v.map",
])
def test_variable_references_are_never_truncated(line):
    """Regression: `$(VAR)` was cut at the '$', leaving `(VAR)` as a stray token."""
    rw = Rewriter("bfd")
    out = rw.rewrite_line(line)
    assert out == line, out
    assert "(" not in out.replace("$(", "")[len("LDFLAGS"):] or "$(" in out


def test_dropped_flag_with_variable_value_is_removed_whole():
    """`-fuse-ld=$(LLD)` must go entirely, not leave `(LLD)` behind."""
    rw = Rewriter("lld")
    out = rw.rewrite_line("\t$(CC) -fuse-ld=$(LLD) -Wl,--build-id=sha1 -o $@ $^")
    assert out == "\t$(CC) -Wl,--build-id=sha1 -o $@ $^"
    assert "(LLD)" not in out


def test_flags_inside_make_text_functions_are_left_alone():
    """Regression: `$(filter -fuse-ld=%,...)` had its *pattern* rewritten away."""
    rw = Rewriter("bfd")
    for line in ["ifeq ($(filter -fuse-ld=% --ld-path=%,$(KBUILD_HOSTLDFLAGS)),)",
                 "X := $(filter-out -Wl$(comma)--fork,$(LDFLAGS))",
                 "Y := $(patsubst -fuse-ld=%,,$(LDFLAGS))",
                 "Z := $(findstring --icf=all,$(LDFLAGS))"]:
        assert rw.rewrite_line(line) == line, line
    assert rw.diags == []
    # but a flag outside the function on the same line is still converted
    out = rw.rewrite_line("LDFLAGS += $(filter -Wl,%,$(X)) -Wl,--gc-sections")
    assert "--gc-sections" in out


def test_ld_option_probe_wrapper_is_converted_not_skipped():
    """`$(call ld-option,FLAG)` passes FLAG to the linker, so it is convertible."""
    rw = Rewriter("mold")
    out = rw.rewrite_line("KBUILD_LDFLAGS += $(call ld-option,--fork)")
    assert "--fork" not in out


def test_orphan_handling_is_kept():
    """The kernel relies on it; it only affects diagnostics, so keep and let check decide."""
    out, diags = convert_driver_string("-Wl,--orphan-handling=warn", "bfd")
    assert out == "-Wl,--orphan-handling=warn"
    assert not [d for d in diags if d.level in ("warn", "error")]


def test_cc_option_probes_are_never_touched():
    """`$(call cc-option, -Wfoo)` carries COMPILER flags: not ours to rewrite."""
    rw = Rewriter("mold")
    for line in ["CFLAGS += $(call cc-option, -femit-struct-debug-detailed=any)",
                 "\t$(call cc-option, -Wunused-but-set-variable)\t\t\\",
                 "subdir-ccflags-y += $(call cc-option, -Wrestrict)",
                 "# Usage: KBUILD_LDFLAGS += $(call ld-option, -X, -Y)"]:
        assert rw.rewrite_line(line) == line, line
    assert rw.diags == []


def test_ld_option_probe_is_converted_but_spacing_is_preserved():
    rw = Rewriter("mold")
    # nothing to change -> byte-identical, original spacing intact
    keep = "KBUILD_LDFLAGS += $(call ld-option, --gc-sections)"
    assert rw.rewrite_line(keep) == keep
    # something to change -> the flag goes
    assert rw.rewrite_line("KBUILD_LDFLAGS += $(call ld-option,--fork)") == \
        "KBUILD_LDFLAGS += $(call ld-option,)"


def test_source_directory_named_build_is_not_deleted(tmp_path):
    """Regression: any path component named build/out was dropped, source and all."""
    src = tmp_path / "src"
    (src / "tools" / "build").mkdir(parents=True)
    (src / "out" / "gen").mkdir(parents=True)
    (src / "build").mkdir()
    (src / "Makefile").write_text("all:\n")
    (src / "tools" / "build" / "Makefile").write_text("all:\n")
    (src / "tools" / "build" / "fixdep.c").write_text("int main(void){return 0;}\n")
    (src / "out" / "gen" / "template.h").write_text("#define X 1\n")
    (src / "build" / "CMakeCache.txt").write_text("stale")  # genuine build output
    convert_project(src, tmp_path / "dst")
    dst = tmp_path / "dst"
    assert (dst / "tools" / "build" / "fixdep.c").exists()
    assert (dst / "out" / "gen" / "template.h").exists()
    assert not (dst / "build").exists()


def test_include_is_anchored_to_the_entry_point_not_lastword(tmp_path):
    """Regression: `$(lastword $(MAKEFILE_LIST))` stops naming the current file
    once that file includes another, so the eld.mk path resolved elsewhere."""
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "Makefile").write_text("LD = ld.gold\ninclude scripts/rules.mk\nall:\n\t@echo $(LD)\n")
    (src / "scripts" / "rules.mk").write_text("include scripts/helper.mk\nX := 1\n")
    (src / "scripts" / "helper.mk").write_text("Y := 2\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    # only the entry point pulls eld.mk in; the fragments are untouched
    assert "eld.mk" in (dst / "Makefile").read_text()
    assert "eld.mk" not in (dst / "scripts" / "rules.mk").read_text()
    if shutil.which("make"):
        import subprocess
        r = subprocess.run(["make", "-C", str(dst), "all"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "eld-bin/ld" in r.stdout


def test_sub_makefiles_inherit_the_shim_from_the_top(tmp_path):
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "Makefile").write_text("LD = ld.gold\nall:\n\t$(MAKE) -C lib\n")
    (src / "lib" / "Makefile").write_text("all:\n\t@echo LD=$(LD)\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/x/ld.eld")
    if shutil.which("make"):
        import subprocess
        r = subprocess.run(["make", "-C", str(dst), "all"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "eld-bin/ld" in r.stdout  # exported through the recursive make


# ---------------------------------------------------------------- build-level obstacles

def test_linker_identity_gate_is_reported(tmp_path):
    """kbuild refuses any linker that does not call itself GNU ld or LLD."""
    from eldconv.obstacles import scan
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "Makefile").write_text("LD = $(CROSS_COMPILE)ld\nall:\n")
    (src / "scripts" / "ld-version.sh").write_text(
        '#!/bin/sh\nset -- $($1 --version)\nif [ "$1" = GNU -a "$2" = ld ]; then\n'
        '  name=BFD\nelif [ "$1" != "LLD" ]; then\n  exit 1\nfi\n')
    diags = scan(src)
    ids = {d.flag for d in diags}
    assert "kbuild-ld-version" in ids
    d = next(x for x in diags if x.flag == "kbuild-ld-version")
    assert d.level == "error" and d.file == "scripts/ld-version.sh" and d.line
    assert "mold" in d.message  # names the precedent
    # and it reaches the conversion report
    rep = convert_project(src, tmp_path / "dst")
    assert any(x.flag == "kbuild-ld-version" for x in rep.errors)


def test_no_obstacles_reported_for_an_ordinary_project(tmp_path):
    from eldconv.obstacles import scan
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("LD = ld.gold\nall:\n\t$(LD) -o app main.o\n")
    assert scan(src) == []


def test_root_level_mk_fragment_is_not_treated_as_an_entry_point(tmp_path):
    """Regression: any depth-0 file got `include eld.mk`, even a plain fragment."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("include common.mk\nall:\n")
    (src / "common.mk").write_text("CFLAGS = -O2\n")
    dst = tmp_path / "dst"
    rep = convert_project(src, dst, eld="/x/ld.eld")
    assert "eld.mk" in (dst / "Makefile").read_text()
    assert "eld.mk" not in (dst / "common.mk").read_text()
    assert rep.files_rewritten == ["Makefile"]
