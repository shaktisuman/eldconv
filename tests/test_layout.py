"""Layout comparison (layout.py): segments, permissions, alignment, and -- for an image a
linker script places -- base address, order, load addresses and the symbols that mark
section edges."""
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from eldconv.elf import ELF, PT_LOAD, PT_TLS, open_elf
from eldconv.elfcheck import compare, validate
from eldconv.layout import (PT_GNU_PROPERTY, _moved, compare_layout, layout)

CC = shutil.which("cc") or shutil.which("gcc")
LD = shutil.which("ld")
needs_cc = pytest.mark.skipif(not (CC and LD), reason="needs cc and ld")
PT_GNU_STACK, PT_GNU_RELRO = 0x6474E551, 0x6474E552


def _cc(tmp: Path, out: str, *args: str) -> Path:
    subprocess.run([CC, *args, "-o", str(tmp / out)], cwd=tmp, check=True, capture_output=True)
    return tmp / out


def _what(findings, level=None):
    return {f.what for f in findings if level is None or f.level == level}


def _phdr_off(e: ELF, i: int) -> int:
    phoff, = struct.unpack_from("<Q", e.data, 0x20)
    phentsize, = struct.unpack_from("<H", e.data, 0x36)
    return phoff + i * phentsize


def _drop_segment(src: Path, dst: Path, ptype: int) -> bool:
    """Copy `src` to `dst` with its `ptype` program header turned into PT_NULL."""
    data = bytearray(src.read_bytes())
    e = ELF.from_bytes(bytes(data))
    idx = [i for i, p in enumerate(e.phdrs) if p.type == ptype]
    for i in idx:
        struct.pack_into("<I", data, _phdr_off(e, i), 0)
    dst.write_bytes(bytes(data))
    return bool(idx)


@pytest.fixture
def src(tmp_path):
    (tmp_path / "main.c").write_text("__thread int t = 1;\nint main(void){return t;}\n")
    return tmp_path


def _host_ok(path: Path) -> None:
    with open_elf(path) as e:
        if not e.is64 or not e.le:
            pytest.skip("patches assume a 64-bit little-endian host")


# ------------------------------------------------------------------------- generic

@needs_cc
def test_same_link_has_no_layout_differences(src):
    a = _cc(src, "a", "main.c")
    with open_elf(a) as r, open_elf(a) as n:
        assert compare_layout(r, n) == [] and compare_layout(r, n, scripted=True) == []


@needs_cc
def test_layout_lists_segments_and_sections(src):
    with open_elf(_cc(src, "a", "main.c")) as e:
        lay = layout(e)
    loads = [s for s in lay["segments"] if s["type"] == "LOAD"]
    assert loads and any("X" in s["flags"] for s in loads)
    text = next(s for s in lay["sections"] if s["name"] == ".text")
    assert "X" in text["segment"] and text["size"] > 0 and text["lma"] == text["addr"]
    assert any(".text" in s["sections"] for s in loads)


@needs_cc
def test_read_only_data_mapped_with_code(src):
    """GNU ld's -z separate-code keeps headers and rodata out of the executable segment."""
    a = _cc(src, "a", "main.c", "-Wl,-z,separate-code")
    b = _cc(src, "b", "main.c", "-Wl,-z,noseparate-code")
    with open_elf(a) as r, open_elf(b) as n:
        found = compare_layout(r, n)
    got_x = [f for f in found if f.what == "permissions"]
    assert got_x and got_x[0].level == "warn" and "separate-code" in got_x[0].message
    assert "segments" in _what(found, "info")


@needs_cc
def test_smaller_segment_alignment(src):
    a = _cc(src, "a", "main.c", "-Wl,-z,max-page-size=0x10000")
    b = _cc(src, "b", "main.c", "-Wl,-z,max-page-size=0x1000")
    with open_elf(a) as r, open_elf(b) as n:
        assert "alignment" in _what(compare_layout(r, n), "warn")
    with open_elf(b) as r, open_elf(a) as n:     # larger is fine
        assert "alignment" not in _what(compare_layout(r, n))


@needs_cc
def test_lost_tls_segment_is_an_error(src):
    a = _cc(src, "a", "main.c")
    _host_ok(a)
    assert _drop_segment(a, src / "b", PT_TLS)
    with open_elf(a) as r, open_elf(src / "b") as n:
        found = compare_layout(r, n)
    assert any(f.what == "segments" and f.level == "error" and "PT_TLS" in f.message for f in found)


@needs_cc
def test_lost_gnu_property_segment(src):
    a = _cc(src, "a", "main.c")
    _host_ok(a)
    if not _drop_segment(a, src / "b", PT_GNU_PROPERTY):
        pytest.skip("the host toolchain writes no PT_GNU_PROPERTY")
    with open_elf(a) as r, open_elf(src / "b") as n:
        assert any("PT_GNU_PROPERTY" in f.message and f.level == "warn" for f in compare_layout(r, n))


@needs_cc
def test_missing_gnu_stack(src):
    """eld writes PT_GNU_STACK only for an explicit -z noexecstack: without it the loader
    assumes an executable stack, and glibc 2.41+ refuses to dlopen the library."""
    (src / "f.c").write_text("int f(void){return 1;}\n")
    a = _cc(src, "liba.so", "-shared", "-fPIC", "f.c", "-Wl,-soname,liba.so")
    _host_ok(a)
    assert _drop_segment(a, src / "libb.so", PT_GNU_STACK)
    with open_elf(src / "libb.so") as n:
        assert any(f.what == "stack" and f.level == "warn" and "dlopen" in f.message for f in validate(n))
    with open_elf(a) as r, open_elf(src / "libb.so") as n:
        assert ("error", "stack") in {(f.level, f.what) for f in compare(r, n)}
    with open_elf(a) as n:
        assert "stack" not in _what(validate(n))
    # glibc's default stack is not executable on AArch64: nothing to report there
    data = bytearray((src / "libb.so").read_bytes())
    struct.pack_into("<H", data, 18, 183)
    (src / "libc64.so").write_bytes(bytes(data))
    rdata = bytearray(a.read_bytes())
    struct.pack_into("<H", rdata, 18, 183)
    (src / "liba64.so").write_bytes(bytes(rdata))
    with open_elf(src / "liba64.so") as r, open_elf(src / "libc64.so") as n:
        assert "stack" not in _what(validate(n)) and "stack" not in _what(compare(r, n))


@needs_cc
def test_sections_that_leave_relro(src):
    a = _cc(src, "a", "main.c", "-Wl,-z,relro")
    _host_ok(a)
    data = bytearray(a.read_bytes())
    e = ELF.from_bytes(bytes(data))
    i = next(i for i, p in enumerate(e.phdrs) if p.type == PT_GNU_RELRO)
    struct.pack_into("<Q", data, _phdr_off(e, i) + 40, 0)      # p_memsz
    (src / "b").write_bytes(bytes(data))
    with open_elf(a) as r, open_elf(src / "b") as n:
        assert "relro" in _what(compare_layout(r, n), "warn")


def test_moved_finds_the_sections_out_of_order():
    assert _moved(list("abcdef"), list("abcdef")) == []
    assert _moved(list("abcdef"), list("abdecf")) == ["c"]
    assert sorted(_moved(list("abcd"), list("dcba"))) == ["a", "b", "c"]
    assert _moved(["a", "b"], ["b"]) == []


# ------------------------------------------------------------------ scripted images

IMAGE_C = """
int data_words[64] = {1};
int bss_words[128];
static const char message[] = "hello";
const char *msg(void) { return message; }
void _start(void) { for (;;) { data_words[0] += bss_words[1]; } }
"""


def _image(tmp: Path, name: str, base: int = 0x100000, lma: int = 0x800000, data_flags: int = 6,
           swap: bool = False, empty: bool = False, bss_marker_inside: bool = False) -> Path:
    obj = tmp / "image.o"
    if not obj.exists():
        (tmp / "image.c").write_text(IMAGE_C)
        subprocess.run([CC, "-c", "-O1", "-ffreestanding", "-fno-pic", "-fno-asynchronous-unwind-tables",
                        "image.c", "-o", "image.o"], cwd=tmp, check=True, capture_output=True)
    rodata = "  .rodata : { *(.rodata*) } :text\n"
    data = (f"  .data : AT({lma:#x}) {{ __mydata_start = .; "
            + ("__mydata_end = .; *(.data*)" if empty else "*(.data*) __mydata_end = .;") + " } :data\n")
    bss = ("  .bss : { " + (". += 16; " if bss_marker_inside else "")
           + "__bss_start = .; *(.bss*) *(COMMON) __bss_end = .; } :data\n")
    script = (f"ENTRY(_start)\nPHDRS {{ text PT_LOAD FLAGS(5); data PT_LOAD FLAGS({data_flags}); }}\n"
              f"SECTIONS {{\n  . = {base:#x};\n  .text : {{ *(.text*) }} :text\n"
              + ("" if swap else rodata) + "  . = ALIGN(0x1000);\n" + data + bss
              + (rodata.replace(":text", ":data") if swap else "")
              + "  /DISCARD/ : { *(.note*) *(.comment) *(.eh_frame*) }\n}\n")
    (tmp / f"{name}.ld").write_text(script)
    subprocess.run([LD, "-T", f"{name}.ld", "image.o", "-o", name], cwd=tmp, check=True, capture_output=True)
    return tmp / name


def _scripted(tmp, ref_kw, new_kw, scripted=True):
    r, n = _image(tmp, "ref", **ref_kw), _image(tmp, "new", **new_kw)
    with open_elf(r) as a, open_elf(n) as b:
        return compare_layout(a, b, scripted=scripted)


@needs_cc
def test_scripted_same_layout(tmp_path):
    assert _scripted(tmp_path, {}, {}) == []


@needs_cc
def test_scripted_base_address(tmp_path):
    found = _scripted(tmp_path, {}, {"base": 0x200000})
    assert "base" in _what(found, "error") and "placement" in _what(found, "info")
    assert "base" in _what(_scripted(tmp_path, {}, {"base": 0x200000}, scripted=False), "info")


@needs_cc
def test_scripted_load_address(tmp_path):
    found = _scripted(tmp_path, {}, {"lma": 0x900000})
    lma = [f for f in found if f.what == "lma"]
    assert lma and lma[0].level == "error" and ".data" in lma[0].message
    assert "lma" in _what(_scripted(tmp_path, {}, {"lma": 0x900000}, scripted=False), "warn")


@needs_cc
def test_scripted_order(tmp_path):
    found = _scripted(tmp_path, {}, {"swap": True})
    assert any(f.what == "order" and f.level == "warn" and ".rodata" in f.message for f in found)


@needs_cc
def test_scripted_permissions(tmp_path):
    found = _scripted(tmp_path, {}, {"data_flags": 4})
    assert any(f.what == "permissions" and f.level == "error" and ".data" in f.message for f in found)


@needs_cc
def test_scripted_empty_range(tmp_path):
    found = _scripted(tmp_path, {}, {"empty": True})
    assert any(f.what == "ranges" and "__mydata_start..__mydata_end" in f.message for f in found)


@needs_cc
def test_scripted_boundary_symbol(tmp_path):
    found = _scripted(tmp_path, {}, {"bss_marker_inside": True})
    off = [f for f in found if f.what == "boundaries"]
    assert off and off[0].level == "error" and "__bss_start" in off[0].message
    # an image the linker placed: the linker's own symbols are only information
    loose = _scripted(tmp_path, {}, {"bss_marker_inside": True}, scripted=False)
    assert ("info", "boundaries") in {(f.level, f.what) for f in loose}


@needs_cc
def test_compare_passes_scripted_through(tmp_path):
    r, n = _image(tmp_path, "ref"), _image(tmp_path, "new", base=0x200000)
    with open_elf(r) as a, open_elf(n) as b:
        assert ("error", "base") in {(f.level, f.what) for f in compare(a, b, scripted=True)}
        assert ("error", "base") not in {(f.level, f.what) for f in compare(a, b)}
        assert any(p.type == PT_LOAD for p in b.phdrs)
