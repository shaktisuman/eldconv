
from eldconv.flags import (LinkArg, emit_linker_item, emit_raw, emit_wl, emit_xlinker,
                           parse_linker_item, parse_raw, parse_wl, parse_xlinker)


def names(args):
    return [(a.name, a.value, a.sep) for a in args]


def test_parse_wl_simple():
    assert names(parse_wl("-Wl,--gc-sections")) == [("--gc-sections", None, "")]


def test_parse_wl_multi_and_comma_value():
    args = parse_wl("-Wl,--gc-sections,-Map,out.map,--as-needed")
    assert names(args) == [("--gc-sections", None, ""), ("-Map", "out.map", ","), ("--as-needed", None, "")]


def test_parse_wl_equals_value():
    assert names(parse_wl("-Wl,--icf=all")) == [("--icf", "all", "=")]
    assert names(parse_wl("-Wl,-Map=out.map")) == [("-Map", "out.map", "=")]


def test_parse_wl_z_forms():
    assert names(parse_wl("-Wl,-z,now")) == [("-z", "now", ",")]
    assert names(parse_wl("-Wl,-znow")) == [("-z", "now", "")]
    assert names(parse_wl("-Wl,-z,max-page-size=4096")) == [("-z", "max-page-size=4096", ",")]


def test_parse_wl_attached_short():
    assert names(parse_wl("-Wl,-lm,-L/usr/lib,-O2")) == [("-l", "m", ""), ("-L", "/usr/lib", ""), ("-O", "2", "")]


def test_parse_wl_rpath_origin():
    assert names(parse_wl("-Wl,-rpath,$ORIGIN/lib")) == [("-rpath", "$ORIGIN/lib", ",")]


def test_parse_wl_positional():
    args = parse_wl("-Wl,--start-group,foo.a,bar.a,--end-group")
    assert args[1].positional and args[1].name == "foo.a"
    assert names(args)[-1] == ("--end-group", None, "")


def test_parse_wl_missing_value_at_end():
    # dangling value flag must not crash
    assert names(parse_wl("-Wl,-Map")) == [("-Map", None, "")]


def test_emit_wl_roundtrip():
    src = "-Wl,--gc-sections,-Map,out.map,--icf=all,-znow,-z,relro"
    assert emit_wl(parse_wl(src)) == src


def test_emit_wl_empty():
    assert emit_wl([]) == ""


def test_parse_linker_item_comma_and_shell():
    assert names(parse_linker_item("-z,now")) == [("-z", "now", ",")]
    assert names(parse_linker_item("SHELL:--thinlto-jobs=4 --as-needed")) == [("--thinlto-jobs", "4", "="), ("--as-needed", None, "")]


def test_emit_linker_item():
    a = parse_linker_item("-z,now")
    assert emit_linker_item(a, shell=False) == "LINKER:-z,now"
    assert emit_linker_item(a, shell=True) == "LINKER:SHELL:-z now"
    assert emit_linker_item([], shell=False) == ""


def test_xlinker_pairs():
    args = parse_xlinker(["-Map", "out.map", "--gc-sections"])
    assert names(args) == [("-Map", "out.map", " "), ("--gc-sections", None, "")]
    assert emit_xlinker(args) == "-Xlinker -Map -Xlinker out.map -Xlinker --gc-sections"


def test_parse_raw_argv():
    argv = ["-o", "a.out", "-znow", "--icf=all", "crt1.o", "main.o", "-lc", "-L/lib", "-plugin", "x.so"]
    args = parse_raw(argv)
    assert names(args)[:4] == [("-o", "a.out", " "), ("-z", "now", ""), ("--icf", "all", "="), ("crt1.o", None, "")]
    assert emit_raw(args) == argv


def test_linkarg_parts():
    assert LinkArg("--icf", "all", "=").parts() == ["--icf=all"]
    assert LinkArg("-l", "m", "").parts() == ["-lm"]
    assert LinkArg("-Map", "x", ",").parts() == ["-Map", "x"]
    assert LinkArg("--gc-sections").parts() == ["--gc-sections"]
