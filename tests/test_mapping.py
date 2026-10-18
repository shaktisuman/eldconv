import pytest

from eldconv import LINKERS
from eldconv.flags import LinkArg, parse_wl
from eldconv.mapping import RULES, Action, convert_arg, convert_args, find_rule


def conv(spec, linker):
    return convert_args(parse_wl(spec), linker)


@pytest.mark.parametrize("linker", LINKERS)
def test_gc_sections_kept_everywhere(linker):
    out, d = conv("-Wl,--gc-sections", linker)
    assert [a.name for a in out] == ["--gc-sections"]
    assert d == []


@pytest.mark.parametrize("linker", ["lld", "gold", "mold"])
def test_icf_unsupported(linker):
    out, d = conv("-Wl,--icf=all", linker)
    assert out == []
    assert d[0].level == "error" and "identical-code-folding" in d[0].message


def test_icf_unknown_for_bfd_is_kept_with_info():
    # bfd has no --icf; if it appears the converter can't classify it
    out, d = conv("-Wl,--icf=all", "bfd")
    assert [a.name for a in out] == ["--icf"]
    assert d[0].level == "info"


def test_fuse_ld_and_ld_path_dropped():
    for name in ("-fuse-ld", "--ld-path"):
        out, d = convert_arg(LinkArg(name, "lld", "="), "lld")
        assert out == [] and d[0].level == "warn" and "shim" in d[0].message


@pytest.mark.parametrize("val", ["now", "lazy", "relro", "norelro", "defs", "muldefs", "noexecstack", "max-page-size=4096"])
def test_z_keywords_kept(val):
    out, d = conv(f"-Wl,-z,{val}", "bfd")
    assert [(a.name, a.value) for a in out] == [("-z", val)]
    assert d == []


def test_z_pack_relative_relocs_unsupported():
    for linker in ("lld", "mold", "bfd"):
        out, d = conv("-Wl,-z,pack-relative-relocs", linker)
        assert out == [] and d[0].level == "error" and "RELR" in d[0].message


def test_z_cet_dropped():
    out, d = conv("-Wl,-z,ibt,-z,shstk,-z,cet-report=error", "lld")
    assert out == [] and all(x.level == "warn" for x in d) and len(d) == 3


def test_z_unknown_keyword_kept_tentatively():
    out, d = conv("-Wl,-z,some-new-keyword", "lld")
    assert [(a.name, a.value) for a in out] == [("-z", "some-new-keyword")]
    assert d[0].level == "info" and "tentative" in d[0].message


def test_mold_specific_dropped():
    out, d = conv("-Wl,--fork,--perf,--quick-exit,--no-fork,--stats", "mold")
    assert out == [] and len(d) == 5 and all(x.level == "warn" for x in d)


def test_mold_repro_renamed_with_value():
    out, d = conv("-Wl,--repro", "mold")
    assert out[0].parts() == ["--reproduce=eld-repro.tar"]
    assert d[0].level == "info" and "renamed" in d[0].message


def test_mold_flags_are_not_mold_rules_for_lld():
    # --fork means nothing to lld; converter should treat as unknown, not mold-drop
    out, d = conv("-Wl,--fork", "lld")
    assert [a.name for a in out] == ["--fork"] and d[0].level == "info"


def test_gold_incremental_glob_dropped():
    out, d = conv("-Wl,--incremental-full,--incremental-base,--incremental", "gold")
    assert out == [] and len(d) == 3


def test_plugin_options_are_kept_because_eld_accepts_them():
    """Verified against ld.eld 24.0: -plugin/-plugin-opt/--plugin-opt are accepted.
    The shim still strips GCC's collect2 plugin arguments."""
    for spec in ("-Wl,-plugin-opt=O2", "-Wl,--plugin-opt=O2", "-Wl,-plugin,x.so"):
        out, d = conv(spec, "gold")
        assert out and not [x for x in d if x.level == "error"], spec


def test_bfd_specific_dropped():
    # --no-warn-rwx-segments is accepted by eld 24.0, so only the other two go
    out, d = conv("-Wl,--ctf-variables,--gc-keep-exported", "bfd")
    assert out == [] and len(d) == 2
    out, d = conv("-Wl,--no-warn-rwx-segments", "bfd")
    assert [a.name for a in out] == ["--no-warn-rwx-segments"] and d == []


def test_lld_lto_options_dropped():
    # --thinlto-jobs and --lto-O<n> are accepted by eld 24.0; the rest have no equivalent
    out, d = conv("-Wl,--thinlto-cache-dir=/tmp/c,--lto-CGO2", "lld")
    assert out == [] and len(d) == 2
    out, d = conv("-Wl,--lto-O3", "lld")
    assert [a.name for a in out] == ["--lto-O3"] and d == []
    out, d = conv("-Wl,--thinlto-jobs=8", "lld")
    assert [a.name for a in out] == ["--thinlto-jobs"]


def test_lto_options_is_renamed_to_elds_spelling():
    """eld calls it --flto-options, not --lto-options."""
    out, d = conv("-Wl,--lto-options=foo", "bfd")
    assert out[0].parts() == ["--flto-options=foo"]
    assert d[0].level == "info" and "renamed" in d[0].message


def test_compress_debug_sections_unsupported_all():
    for linker in LINKERS:
        out, d = conv("-Wl,--compress-debug-sections=zlib", linker)
        assert out == [] and d[0].level == "error"


def test_double_dash_map_renamed():
    out, d = conv("-Wl,--Map=out.map", "bfd")
    assert out[0].parts() == ["-Map=out.map"]


def test_unknown_flag_kept_with_info():
    out, d = conv("-Wl,--totally-made-up=1", "lld")
    assert out[0].parts() == ["--totally-made-up=1"] and d[0].level == "info"


def test_positional_untouched():
    out, d = convert_args([LinkArg("foo.o", positional=True)], "bfd")
    assert out[0].name == "foo.o" and d == []


def test_mixed_sequence_preserves_order():
    out, d = conv("-Wl,--as-needed,--icf=safe,-lm,--gc-sections,--fork", "mold")
    assert [a.parts()[0] for a in out] == ["--as-needed", "-lm", "--gc-sections"]


def test_rule_table_sanity():
    # every rule has a valid action; RENAME rules carry a target
    for r in RULES:
        assert isinstance(r.action, Action)
        if r.action is Action.RENAME:
            assert r.target
        assert r.linkers, r


def test_find_rule_first_match_wins():
    # specific -z rule beats the generic -z catch-all
    r = find_rule(LinkArg("-z", "pack-relative-relocs", ","), "lld")
    assert r.action is Action.UNSUPPORTED
    r = find_rule(LinkArg("-z", "now", ","), "lld")
    assert r.action is Action.KEEP and not r.tentative


def test_z_undefs_and_textoff_dropped_with_semantic_loss():
    """Verified against ld.eld 24.0: both are rejected ("Unrecognized option"),
    unlike their close relatives -z defs/notext, which eld does accept."""
    out, d = conv("-Wl,-z,undefs", "bfd")
    assert out == [] and d[0].level == "error"
    out, d = conv("-Wl,-z,textoff", "bfd")
    assert out == [] and d[0].level == "error" and "notext" in d[0].message


def test_z_origin_dropped_harmlessly():
    """Verified against ld.eld 24.0: rejected, but the DF_ORIGIN hint is safe to lose."""
    out, d = conv("-Wl,-z,origin", "bfd")
    assert out == [] and d[0].level == "warn"


def test_z_nognustack_and_noseparate_code_kept():
    """Verified against ld.eld 24.0: both are accepted, even though --help only
    documents the hyphenated "no-separate-code" spelling (which eld itself rejects)."""
    for val in ("nognustack", "noseparate-code", "separate-code"):
        out, d = conv(f"-Wl,-z,{val}", "bfd")
        assert [(a.name, a.value) for a in out] == [("-z", val)]
        assert not [x for x in d if x.level == "error"], val


def test_lld_lto_flags_eld_actually_supports():
    """Verified against ld.eld 24.0: --lto-emit-asm/--lto-emit-llvm (bare) and
    --lto-partitions=N are all accepted, unlike their DROP'd LLD-only neighbours."""
    out, d = conv("-Wl,--lto-emit-asm,--lto-emit-llvm", "lld")
    assert [a.name for a in out] == ["--lto-emit-asm", "--lto-emit-llvm"] and d == []
    out, d = conv("-Wl,--lto-partitions=4", "lld")
    assert [a.parts() for a in out] == [["--lto-partitions=4"]] and d == []
    # unaffected neighbours are still dropped
    out, d = conv("-Wl,--lto-whole-program-visibility,--lto-CGO2", "lld")
    assert out == [] and len(d) == 2


def test_no_warn_mismatch_and_warn_once_confirmed_kept():
    """Verified against ld.eld 24.0: both are accepted, not tentative any more."""
    out, d = conv("-Wl,--no-warn-mismatch", "bfd")
    assert [a.name for a in out] == ["--no-warn-mismatch"] and d == []
    out, d = conv("-Wl,--warn-once", "mold")
    assert [a.name for a in out] == ["--warn-once"] and d == []


@pytest.mark.parametrize("spelling", ["--dT", "-dT"])
def test_dT_renamed_to_default_script(spelling):
    """Verified against ld.eld 24.0: -dT/--dT are rejected outright
    ("Unrecognized option") in every form, even though eld lists and accepts
    its --default-script synonym; convert to the working spelling instead of
    silently dropping the script."""
    out, d = convert_args(parse_wl(f"-Wl,{spelling},custom.lds"), "bfd")
    assert [a.parts() for a in out] == [["--default-script=custom.lds"]]
    assert d[0].level == "info" and "renamed" in d[0].message
