from eldconv.converters.cmake import rewrite_cmake_cache_text, rewrite_cmake_text
from eldconv.converters.make import rewrite_make_text
from eldconv.rewrite import Rewriter, convert_driver_string, convert_raw_string


# ---------------------------------------------------------------- driver strings

def test_driver_string_lld():
    s = "-O2 -fuse-ld=lld -Wl,--icf=all -Wl,--gc-sections,-Map,out.map -Xlinker --time-trace -Xlinker --as-needed -lm"
    out, d = convert_driver_string(s, "lld")
    assert out == "-O2 -Wl,--gc-sections,-Map,out.map -Xlinker --as-needed -lm"
    assert {x.flag for x in d} == {"-fuse-ld=lld", "--icf=all", "--time-trace"}


def test_driver_string_untouched_when_nothing_to_do():
    s = "-O2 -g -Wall -Wl,--gc-sections -lpthread"
    out, d = convert_driver_string(s, "bfd")
    assert out == s and d == []


def test_driver_string_keeps_non_linker_double_dash():
    # --sysroot is a driver flag; it's KEEP in the table so untouched
    s = "--sysroot=/opt/sdk -Wl,--fork"
    out, _ = convert_driver_string(s, "mold")
    assert out == "--sysroot=/opt/sdk"


def test_ld_path_removed():
    out, d = convert_driver_string("--ld-path=/usr/bin/ld.lld -Wl,-znow", "lld")
    assert out == "-Wl,-znow" and d[0].flag == "--ld-path=/usr/bin/ld.lld"


def test_xlinker_run_fully_dropped_leaves_no_residue():
    out, _ = convert_driver_string("-Xlinker --fork -Xlinker --perf -o app", "mold")
    assert out == "-o app"


def test_raw_string():
    out, d = convert_raw_string("--gc-sections --icf=safe --no-map-whole-files -o raw.elf main.o", "gold")
    assert out == "--gc-sections -o raw.elf main.o"


def test_bare_flag_only_rewritten_when_rule_is_not_keep():
    rw = Rewriter("gold")
    assert rw.rewrite_line("LDFLAGS = --gc-sections --icf=all") == "LDFLAGS = --gc-sections"
    assert rw.rewrite_line("--help --version") == "--help --version"  # unknown -> untouched


def test_bare_regex_does_not_touch_inside_wl_or_variables():
    rw = Rewriter("mold")
    assert rw.rewrite_line("$(FLAGS)--fork x") == "$(FLAGS)--fork x"
    assert rw.rewrite_line("-Wl,--as-needed,--fork") == "-Wl,--as-needed"


def test_variable_refs_preserved():
    out, _ = convert_driver_string("$(LDFLAGS) ${EXTRA} -Wl,--icf=all", "lld")
    assert out == "$(LDFLAGS) ${EXTRA}"


# ---------------------------------------------------------------- cmake

def test_cmake_rewrite_all_constructs():
    text = """cmake_minimum_required(VERSION 3.16)
set(CMAKE_LINKER_TYPE LLD)
set(CMAKE_LINKER /usr/bin/ld.lld)
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -fuse-ld=lld -Wl,--icf=all -Wl,--gc-sections")
target_link_options(t PRIVATE "-Wl,-Map,t.map" "LINKER:-z,now" "LINKER:SHELL:--time-trace --as-needed")
set_target_properties(t PROPERTIES LINKER_TYPE LLD OUTPUT_NAME tee)
"""
    out, diags = rewrite_cmake_text(text, "lld")
    assert "# eldconv: removed -> set(CMAKE_LINKER_TYPE LLD)" in out
    assert 'set(CMAKE_LINKER "${ELD_SHIM_LD}")' in out
    assert 'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -Wl,--gc-sections")' in out
    assert 'target_link_options(t PRIVATE "-Wl,-Map,t.map" "LINKER:-z,now" "LINKER:SHELL:--as-needed")' in out
    assert "set_target_properties(t PROPERTIES OUTPUT_NAME tee)" in out
    assert "-fuse-ld" not in out and "icf" not in out and "LINKER_TYPE" not in out.replace("# eldconv: removed -> set(CMAKE_LINKER_TYPE LLD)", "")
    kinds = {d.flag for d in diags}
    assert {"CMAKE_LINKER_TYPE", "CMAKE_LINKER", "LINKER_TYPE", "-fuse-ld=lld", "--icf=all", "--time-trace"} <= kinds
    assert all(d.line for d in diags)


def test_cmake_empty_quoted_option_removed():
    out, _ = rewrite_cmake_text('target_link_options(t PRIVATE "-Wl,--icf=all" "-Wl,--gc-sections")', "lld")
    assert out == 'target_link_options(t PRIVATE "-Wl,--gc-sections")'


def test_cmake_generator_expression_untouched():
    line = 'target_link_options(t PRIVATE $<$<CONFIG:Release>:-Wl,--gc-sections>)'
    out, d = rewrite_cmake_text(line, "bfd")
    assert out == line and d == []


def test_cmake_cache_linker_type_removed():
    cache = "CMAKE_LINKER_TYPE:STRING=LLD\nCMAKE_C_FLAGS:STRING=-O2\n"
    out, d = rewrite_cmake_cache_text(cache, "lld")
    assert "CMAKE_LINKER_TYPE" not in out and "CMAKE_C_FLAGS:STRING=-O2" in out
    assert d[0].flag == "CMAKE_LINKER_TYPE"


def test_cmake_compiler_linker_var_removed():
    out, d = rewrite_cmake_text("set(CMAKE_C_COMPILER_LINKER /usr/bin/ld.mold)", "mold")
    assert out.startswith("# eldconv: removed") and d[0].flag == "CMAKE_C_COMPILER_LINKER"


# ---------------------------------------------------------------- make

def test_make_rewrite_ld_and_flags_and_include():
    text = """CC ?= gcc
LD = ld.gold
LDFLAGS := -fuse-ld=gold -Wl,--icf=all -Wl,--gc-sections -Wl,-Map=out.map
app: main.o
\t$(CC) $(LDFLAGS) -o $@ $^
raw: main.o
\t$(LD) --gc-sections --icf=safe --no-map-whole-files -o raw.elf $^
"""
    out, diags = rewrite_make_text(text, "gold", eld_mk_rel="eld.mk")
    assert "LD = $(ELD_SHIM_LD)  # eldconv: was ld.gold" in out
    assert "LDFLAGS := -Wl,--gc-sections -Wl,-Map=out.map" in out
    assert "\t$(LD) --gc-sections -o raw.elf $^" in out
    assert out.rstrip().endswith("include $(dir $(firstword $(MAKEFILE_LIST)))eld.mk\nendif")
    assert "# eldconv: link with eld" in out
    assert any(d.flag == "LD" for d in diags)


def test_make_ld_variants():
    for val in ("$(CROSS)ld.bfd", "/opt/llvm/bin/ld.lld", "mold", "ld", "${TOOLCHAIN}/ld.gold"):
        out, _ = rewrite_make_text(f"LD := {val}\n", "bfd", eld_mk_rel="eld.mk")
        # `:=` is evaluated at once, before eld.mk (included last) defines ELD_SHIM_LD
        assert "LD := $(if $(ELD_SHIM_LD),$(ELD_SHIM_LD)," in out, val
        out, _ = rewrite_make_text(f"LD = {val}\n", "bfd", eld_mk_rel="eld.mk")
        assert "LD = $(ELD_SHIM_LD)" in out, val
    # unrelated LD-ish vars and non-linker values are untouched
    out, _ = rewrite_make_text("LDD = ldd\nLD_LIBRARY_PATH = x\nLD = $(CC)\n", "bfd", eld_mk_rel="eld.mk")
    assert "LDD = ldd" in out and "LD_LIBRARY_PATH = x" in out and "LD = $(CC)" in out


def test_make_export_and_override_ld():
    out, _ = rewrite_make_text("export LD = ld.lld\noverride LD := ld.gold\n", "lld", eld_mk_rel="eld.mk")
    assert out.count("$(ELD_SHIM_LD)") >= 2 and "override LD :=" in out and "export LD =" in out


def test_make_mold_run_removed():
    out, d = rewrite_make_text("app: main.o\n\tmold -run $(CC) $(LDFLAGS) -o $@ $^\n", "mold", eld_mk_rel="eld.mk")
    assert "\t$(CC) $(LDFLAGS) -o $@ $^" in out and d[0].flag == "mold -run"


def test_make_include_relative_for_subdir():
    out, _ = rewrite_make_text("all:\n", "bfd", file="sub/dir/Makefile", eld_mk_rel="../../eld.mk")
    assert "include $(dir $(firstword $(MAKEFILE_LIST)))../../eld.mk" in out
    assert "ifndef ELD_SHIM_LD" in out and out.rstrip().endswith("endif")


def test_make_idempotent_include():
    text = "all:\n\n# eldconv: link with eld\ninclude eld.mk\n"
    out, _ = rewrite_make_text(text, "bfd", eld_mk_rel="eld.mk")
    assert out.count("include eld.mk") == 1


# ---------------------------------------------------------------- regressions

def test_dropped_value_flag_takes_its_value_with_it():
    """A dropped option must not leave its value behind as a bogus input file."""
    cases = [("-Wl,--section-ordering-file,order.txt -Wl,--gc-sections", "gold", "-Wl,--gc-sections"),
             ("-Wl,--icf-iterations,3 -Wl,--gc-sections", "gold", "-Wl,--gc-sections"),
             ("-Wl,--call-graph-ordering-file,cg.txt", "lld", ""),
             ("-Wl,--mllvm,-inline-threshold=0", "lld", ""),
             ("-Wl,--spare-dynamic-tags,5", "mold", ""),
             ("-Xlinker --chroot -Xlinker /sysroot -Xlinker --gc-sections", "lld", "-Xlinker --gc-sections")]
    for src, linker, want in cases:
        assert convert_driver_string(src, linker)[0] == want, src


def test_value_flag_does_not_swallow_a_following_option():
    out, _ = convert_driver_string("-Wl,--icf-iterations,--gc-sections", "gold")
    assert out == "-Wl,--gc-sections"
    out, _ = convert_driver_string("-Wl,-Map,--gc-sections", "bfd")
    assert out == "-Wl,-Map,--gc-sections"  # -Map keeps its (odd) value only if not an option
    from eldconv.flags import parse_wl
    args = parse_wl("-Wl,-Map,--gc-sections")
    assert [a.name for a in args] == ["-Map", "--gc-sections"]


def test_bare_flags_ignored_on_lines_without_linker_context():
    """`python gen.py --stats` must not be mistaken for linker options."""
    rw = Rewriter("mold")
    for line in ["\tpython3 tools/gen.py --stats --perf out.h",
                 "\tcurl --fork https://example.invalid",
                 "DOCS_FLAGS = --stats"]:
        assert rw.rewrite_line(line) == line
    assert rw.diags == []
    # still converted when the line is clearly about linking
    assert rw.rewrite_line("\t$(LD) --fork -o app main.o") == "\t$(LD) -o app main.o"
    assert rw.rewrite_line("LDFLAGS += --perf") == "LDFLAGS +="


def test_driver_string_api_does_not_require_context():
    # `eldconv flags '--icf=all'` has no surrounding build-file context
    assert convert_driver_string("--icf=all --gc-sections", "lld")[0] == "--gc-sections"


def test_json_presets_stay_valid_json():
    """CMakePresets.json is rewritten as text; emptied values must remain valid JSON."""
    import json
    src = ('{\n  "cacheVariables": {\n'
           '    "CMAKE_EXE_LINKER_FLAGS": "-Wl,--fork",\n'
           '    "CMAKE_SHARED_LINKER_FLAGS": "-Wl,--perf" }\n}\n')
    rw = Rewriter("mold")
    out = rw.rewrite_text(src, "CMakePresets.json")
    json.loads(out)
    assert "--fork" not in out and "--perf" not in out


# ---------------------------------------------------------------- encoding / line endings

def test_crlf_lines_stay_crlf():
    from eldconv.converters.make import rewrite_make_text
    out, _ = rewrite_make_text("LDFLAGS := -Wl,--fork -Wl,--gc-sections\r\nall:\r\n", "mold")
    assert out.split("\n")[0] == "LDFLAGS := -Wl,--gc-sections\r"
    assert "\r" in out.split("\n")[1]


def test_cmake_emptied_argument_is_removed_not_left_as_empty_string():
    """`set(X "" "-Wl,..")` would make the value a list and leak a ';' into the link line."""
    out, _ = rewrite_cmake_text('target_link_options(t PRIVATE "-Wl,--fork" "-Wl,--gc-sections")', "mold")
    assert out == 'target_link_options(t PRIVATE "-Wl,--gc-sections")'
    out, _ = rewrite_cmake_text('set(CMAKE_EXE_LINKER_FLAGS\n  "-Wl,--fork"\n  "-Wl,--gc-sections")', "mold")
    assert '""' not in out and "--gc-sections" in out


def test_json_presets_keep_empty_strings():
    import json
    rw = Rewriter("mold")
    out = rw.rewrite_text('{"cacheVariables": {"CMAKE_EXE_LINKER_FLAGS": "-Wl,--fork"}}\n', "CMakePresets.json")
    assert json.loads(out)["cacheVariables"]["CMAKE_EXE_LINKER_FLAGS"] == ""
