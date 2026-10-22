"""--target: an option eld accepts only for other targets (ARM errata, --pic-veneer,
--relax, -G) is dropped from a conversion for this one, instead of being kept because
some other eld driver knows it and left for `check` to catch."""
import json

import pytest

from eldconv import surface
from eldconv.cli import main
from eldconv.flags import parse_raw, parse_wl
from eldconv.mapping import convert_args


def surf():
    d = {"eld_version": "GNU ld compatible linker - eld 24.0", "targets": ["x86_64", "aarch64", "arm"],
         "options": {"--gc-sections": ["x86_64", "aarch64", "arm"],
                     "--fix-cortex-a53-843419": ["aarch64", "arm"],
                     "--relax": ["x86_64"], "--unique": []},
         "z": {"now": ["x86_64", "aarch64", "arm"], "pac-plt": ["aarch64"]}}
    return surface.Surface.from_dict(d)


@pytest.fixture
def s():
    with surface.use_surface(surf()):
        yield


def conv(spec, target=None, raw=False):
    with surface.use_target(target):
        return convert_args(parse_raw(spec.split()) if raw else parse_wl(spec), "bfd")


def names(out):
    return [a.name if a.name != "-z" else f"-z {a.value}" for a in out]


def test_without_a_target_an_option_any_driver_knows_is_kept(s):
    out, d = conv("-Wl,--fix-cortex-a53-843419,--relax")
    assert names(out) == ["--fix-cortex-a53-843419", "--relax"] and not d


def test_an_option_for_other_targets_is_dropped_with_a_warning_naming_them(s):
    out, d = conv("-Wl,--fix-cortex-a53-843419,--gc-sections", "x86_64")
    assert names(out) == ["--gc-sections"]
    assert [x.level for x in d] == ["warn"]
    assert "only for aarch64, arm links" in d[0].message and "x86_64" in d[0].message


def test_the_options_of_the_target_are_kept(s):
    out, d = conv("-Wl,--fix-cortex-a53-843419,--relax", "aarch64")
    assert names(out) == ["--fix-cortex-a53-843419"] and [x.flag for x in d] == ["--relax"]


def test_z_keywords_follow_the_target_too(s):
    out, d = conv("-Wl,-z,pac-plt,-z,now", "x86_64")
    assert names(out) == ["-z now"] and d[0].level == "warn"


def test_an_option_no_target_accepts_keeps_its_severity(s):
    """--unique is not a target question: eld has it nowhere, and the rule table drops it."""
    with surface.use_target("x86_64"):
        out, d = convert_args([parse_wl("-Wl,--unique")[0]], "bfd")
    assert out == [] and "only for" not in d[0].message


def test_a_links_own_emulation_wins_over_the_target(s):
    out, _ = conv("-m aarch64linux --fix-cortex-a53-843419", "x86_64", raw=True)
    assert names(out) == ["-m", "--fix-cortex-a53-843419"]
    out, _ = conv("-melf_x86_64 --fix-cortex-a53-843419", raw=True)
    assert names(out) == ["-m"]


def test_a_target_the_snapshot_did_not_probe_falls_back_to_any_target(s):
    out, d = conv("-Wl,--fix-cortex-a53-843419", "riscv64")
    assert names(out) == ["--fix-cortex-a53-843419"] and not d


@pytest.mark.parametrize("name,want", [
    ("x86_64", "x86_64"), ("amd64", "x86_64"), ("x86-64", "x86_64"), ("arm64", "aarch64"),
    ("AArch64", "aarch64"), ("aarch64-linux-gnu", "aarch64"), ("armv7a-linux-gnueabihf", "arm"),
    ("thumbv7em", "arm"), ("i686", "i386"), ("i386-linux-gnu", "i386"), ("riscv64-linux-gnu", "riscv64"),
    ("hexagon", "hexagon"), ("x86", None), ("riscv", None), ("mips", None), ("", None),
])
def test_target_names(name, want):
    assert surface.normalize_target(name) == want


@pytest.mark.parametrize("emu,want", [
    ("elf_x86_64", "x86_64"), ("elf32_x86_64", "x86_64"), ("elf_i386", "i386"),
    ("aarch64linux", "aarch64"), ("aarch64elfb", "aarch64"), ("armelf_linux_eabi", "arm"),
    ("armelfb", "arm"), ("elf64lriscv", "riscv64"), ("elf32lriscv_ilp32f", "riscv32"),
    ("hexagonelf", "hexagon"), ("llvm", None), ("elf64ppc", None),
])
def test_emulation_targets(emu, want):
    assert surface.emulation_target(emu) == want


def test_bundled_snapshot_has_target_only_options(bundled_surface):
    assert bundled_surface.accepts("--fix-cortex-a53-843419", target="x86_64") is False
    assert bundled_surface.accepts("--fix-cortex-a53-843419", target="aarch64") is True
    assert bundled_surface.accepts("--fix-cortex-a53-843419") is True
    assert bundled_surface.accepts("--gc-sections", target="hexagon") is True


def test_cli_flags_target(bundled_surface, capsys):
    assert main(["flags", "--target", "x86_64", "-Wl,--pic-veneer,--gc-sections"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "-Wl,--gc-sections" and "only for aarch64, arm links" in err
    assert main(["flags", "--target=arm64", "-Wl,--pic-veneer"]) == 0
    assert capsys.readouterr().out.strip() == "-Wl,--pic-veneer"


def test_cli_rejects_an_unknown_target(capsys):
    assert main(["flags", "--target", "x86", "-Wl,--gc-sections"]) == 2
    assert "both i386 and x86_64" in capsys.readouterr().err


def test_cli_warns_when_the_snapshot_lacks_the_target(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(surf().as_dict()))
    assert main(["flags", "--eld-surface", str(f), "--target", "hexagon", "-Wl,--relax"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "-Wl,--relax" and "has no hexagon" in err


def test_convert_with_a_target(tmp_path, bundled_surface):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("LDFLAGS += -Wl,--gc-sections -Wl,--fix-cortex-a53-843419\n"
                                  "all:\n\t$(CC) $(LDFLAGS) -o app app.c\n")
    (tmp_path / "ld.eld").write_text("#!/bin/sh\n")
    (tmp_path / "ld.eld").chmod(0o755)
    eld = str(tmp_path / "ld.eld")
    assert main(["convert", str(src), str(tmp_path / "x86"), "--eld", eld, "-q", "--target", "x86_64"]) == 0
    assert main(["convert", str(src), str(tmp_path / "arm"), "--eld", eld, "-q", "--target", "aarch64"]) == 0
    assert "fix-cortex" not in (tmp_path / "x86" / "Makefile").read_text()
    assert "-Wl,--fix-cortex-a53-843419" in (tmp_path / "arm" / "Makefile").read_text()
    rep = json.loads((tmp_path / "x86" / "eldconv-report.json").read_text())
    assert any(d["flag"] == "--target" and "x86_64 links" in d["message"] for d in rep["diagnostics"])
