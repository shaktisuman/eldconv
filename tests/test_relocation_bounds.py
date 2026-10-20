"""Linker-script symbols around dynamic relocations, and what check makes of them.

eld (Oct 2026) sets the end of `__pi_rela_start = .; *(.rela .rela*) __pi_rela_end = .;` as if
.rela.dyn were empty, so an arm64 kernel applies no relocations and dies once KASLR moves it.
"""
import struct
from pathlib import Path

from eldconv.checker import empty_relocation_bounds, kconfig, run_check
from eldconv.obstacles import scan

RELA_ADDR = 0xFFFF800080230AA0


def _image(path: Path, rela_size=0x1B6D8, end_offset=None, symtab=True, start="__pi_rela_start",
           end="__pi_rela_end") -> Path:
    """A 64-bit ELF with .rela.dyn at RELA_ADDR and a start/end symbol pair; end_offset=None means correct."""
    end_value = RELA_ADDR + (rela_size if end_offset is None else end_offset)
    shstr = b"\0.rela.dyn\0.symtab\0.strtab\0.shstrtab\0"
    strtab = b"\0" + start.encode() + b"\0" + end.encode() + b"\0"
    syms = bytes(24) + struct.pack("<IBBHQQ", 1, 0, 0, 1, RELA_ADDR, 0) \
        + struct.pack("<IBBHQQ", 2 + len(start), 0, 0, 1, end_value, 0)
    blobs = [bytes(rela_size), syms if symtab else b"", strtab, shstr]
    offs, pos = [], 64
    for b in blobs:
        offs.append(pos)
        pos += len(b)
    shoff = pos

    def sh(name, typ, flags, addr, i, link=0, entsize=0):
        return struct.pack("<IIQQQQIIQQ", shstr.index(name.encode()), typ, flags, addr, offs[i],
                           len(blobs[i]), link, 0, 8, entsize)
    shdrs = bytes(64) + sh(".rela.dyn", 4, 2, RELA_ADDR, 0, entsize=24) \
        + (sh(".symtab", 2, 0, 0, 1, link=3, entsize=24) if symtab else sh(".symtab", 3, 0, 0, 1)) \
        + sh(".strtab", 3, 0, 0, 2) + sh(".shstrtab", 3, 0, 0, 3)
    ehdr = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, 0, shoff, 0, 64, 0, 0,
                                                             64, 5, 4)
    path.write_bytes(ehdr + b"".join(blobs) + shdrs)
    return path


def test_end_symbol_left_at_the_start_is_reported(tmp_path):
    assert empty_relocation_bounds(_image(tmp_path / "vmlinux", end_offset=0)) == [
        (".rela.dyn", "__pi_rela_start", "__pi_rela_end", 0x1B6D8)]


def test_pair_spanning_the_section_is_fine(tmp_path):
    assert empty_relocation_bounds(_image(tmp_path / "vmlinux")) == []


def test_other_spellings_of_the_pair(tmp_path):
    """riscv's kernel uses __rela_dyn_start/__rela_dyn_end."""
    p = _image(tmp_path / "vmlinux", end_offset=0, start="__rela_dyn_start", end="__rela_dyn_end")
    assert [b[1:3] for b in empty_relocation_bounds(p)] == [("__rela_dyn_start", "__rela_dyn_end")]


def test_nothing_to_judge_without_relocations_symbols_or_an_elf(tmp_path):
    assert empty_relocation_bounds(_image(tmp_path / "a", rela_size=0, end_offset=0)) == []
    assert empty_relocation_bounds(_image(tmp_path / "b", end_offset=0, symtab=False)) == []
    (tmp_path / "c").write_text("not elf")
    assert empty_relocation_bounds(tmp_path / "c") == []
    data = _image(tmp_path / "d", end_offset=0).read_bytes()
    for cut in (16, 64, len(data) // 2, len(data) - 1):
        (tmp_path / "t").write_bytes(data[:cut])
        assert empty_relocation_bounds(tmp_path / "t") == []


def test_kconfig_reads_only_kernel_trees(tmp_path):
    (tmp_path / ".config").write_text("# CONFIG_RELOCATABLE is not set\nCONFIG_RANDOMIZE_BASE=y\n")
    assert kconfig(tmp_path) == {}
    (tmp_path / "Kbuild").write_text("")
    assert kconfig(tmp_path) == {"CONFIG_RANDOMIZE_BASE": "y"}
    out = tmp_path / "out"
    out.mkdir()
    (out / ".config").write_text("CONFIG_RANDOMIZE_BASE=n\n")
    assert kconfig(tmp_path, out) == {"CONFIG_RANDOMIZE_BASE": "n"}   # O= build dir wins


def _check(tmp_path, config=None, kernel=True):
    from tests.test_checker import FIX, make_runner
    from eldconv.generator import convert_project
    eld = tmp_path / "ld.eld"
    eld.write_text("#!/bin/sh\nexit 0\n")
    eld.chmod(0o755)
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=str(eld))
    if kernel:
        (dst / "Kbuild").write_text("")
    if config is not None:
        (dst / ".config").write_text(config)
    base = make_runner(link_outputs={"vmlinux": ["-maarch64elf", "-shared", "vmlinux.o"]})

    def runner(cmd, **kw):
        r = base(cmd, **kw)
        if cmd[0] == "make" and "clean" not in cmd:
            _image(Path(kw["env"]["ELDCONV_LINK_LOG"]).parent / "vmlinux", end_offset=0)
        return r
    return run_check(dst, runner=runner)


def test_check_fails_a_kaslr_kernel(tmp_path):
    res = _check(tmp_path, "CONFIG_RELOCATABLE=y\nCONFIG_RANDOMIZE_BASE=y\n")
    assert not res.ok
    assert any("__pi_rela_end" in p and "disable RANDOMIZE_BASE" in p for p in res.problems), res.problems
    assert res.outputs[0]["empty_relocation_bounds"] == [[".rela.dyn", "__pi_rela_start", "__pi_rela_end", 0x1B6D8]]


def test_check_only_warns_while_the_kernel_is_never_moved(tmp_path):
    res = _check(tmp_path, "CONFIG_RELOCATABLE=y\n# CONFIG_RANDOMIZE_BASE is not set\n")
    assert res.ok, res.problems
    assert any("harmless while CONFIG_RANDOMIZE_BASE is off" in w for w in res.warnings)


def test_check_fails_any_other_image(tmp_path):
    res = _check(tmp_path, kernel=False)
    assert not res.ok and any("__pi_rela_end" in p for p in res.problems)


def test_arm64_kernel_tree_gets_the_kaslr_warning(tmp_path):
    (tmp_path / "Kbuild").write_text("")
    (tmp_path / "arch" / "arm64").mkdir(parents=True)
    (tmp_path / "arch" / "arm64" / "Kconfig").write_text("config ARM64\n\tdef_bool y\n\nconfig RANDOMIZE_BASE\n")
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n")
    d = [x for x in scan(tmp_path) if x.flag == "CONFIG_RANDOMIZE_BASE"]
    assert d and d[0].level == "warn" and "disable RANDOMIZE_BASE" in d[0].message
    assert (d[0].file, d[0].line) == ("arch/arm64/Kconfig", 4)
    (tmp_path / "Kbuild").unlink()
    assert not [x for x in scan(tmp_path) if x.flag == "CONFIG_RANDOMIZE_BASE"]
