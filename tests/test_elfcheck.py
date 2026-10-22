"""The ELF reader, structural validation of linked outputs, and comparison with a reference
build (`check --compare`, `eldconv compare`)."""
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from eldconv.checker import REFERENCE_MARK, run_check
from eldconv.cli import main
from eldconv.elf import ELF, PT_LOAD, open_elf
from eldconv.elfcheck import compare, validate
from eldconv.generator import convert_project

CC = shutil.which("cc") or shutil.which("gcc")
needs_cc = pytest.mark.skipif(not (CC and shutil.which("ld")), reason="needs cc and ld")


def _cc(tmp: Path, out: str, *args: str) -> Path:
    subprocess.run([CC, *args, "-o", str(tmp / out)], cwd=tmp, check=True, capture_output=True)
    return tmp / out


@pytest.fixture
def lib_sources(tmp_path):
    (tmp_path / "foo.c").write_text("int foo(void){return 1;}\nint hidden(void){return 2;}\n")
    (tmp_path / "foo.map").write_text("FOO_1 { global: foo; local: *; };\n")
    (tmp_path / "main.c").write_text("int main(void){return 0;}\n")
    return tmp_path


def _significant(findings):
    return [(f.level, f.what) for f in findings if f.level != "info"]


# ---------------------------------------------------------------------------- reader

@needs_cc
def test_reader_on_a_shared_library(lib_sources):
    so = _cc(lib_sources, "libfoo.so", "-shared", "-fPIC", "foo.c", "-Wl,--version-script=foo.map",
             "-Wl,-soname,libfoo.so.1", "-Wl,--build-id", "-Wl,-z,now")
    with open_elf(so) as e:
        assert e.type_name == "DYN" and e.soname == "libfoo.so.1"
        assert "libc.so.6" in e.needed or e.needed == []
        assert e.build_id and e.bind_now and e.has_relro
        assert e.verdef_names >= {"FOO_1"}
        exported = {s.name: e.versions.get(s.index) for s in e.dynsym if s.shndx}
        assert exported["foo"] == "FOO_1" and "hidden" not in exported


@needs_cc
def test_reader_on_an_executable(lib_sources):
    exe = _cc(lib_sources, "app", "main.c")
    with open_elf(exe) as e:
        assert e.interp and e.interp.startswith("/")
        assert e.entry and any(p.type == PT_LOAD for p in e.phdrs)
        assert e.exec_stack is False


def test_reader_rejects_what_is_not_elf(tmp_path):
    (tmp_path / "t").write_bytes(b"\x7fELF" + b"\0" * 10)
    (tmp_path / "s").write_text("#!/bin/sh\n")
    for name in ("t", "s", "missing"):
        with open_elf(tmp_path / name) as e:
            assert e is None
    hdr = bytearray(b"\x7fELF\x02\x01\x01" + b"\0" * 57)
    struct.pack_into("<Q", hdr, 0x20, 0xFFFF)            # program headers past the end
    struct.pack_into("<HH", hdr, 0x36, 56, 4)
    assert ELF.from_bytes(bytes(hdr)) is None
    hdr = bytearray(b"\x7fELF\x02\x01\x01" + b"\0" * 57)
    struct.pack_into("<Q", hdr, 0x28, 0x10)               # section headers inside the header
    struct.pack_into("<HH", hdr, 0x3A, 64, 9)
    assert ELF.from_bytes(bytes(hdr)) is None
    empty = ELF.from_bytes(b"\x7fELF\x02\x01\x01" + b"\0" * 57)
    assert empty is not None and empty.phdrs == [] and validate(empty) == []


# ---------------------------------------------------------------------------- validate

def _patch(src: Path, dst: Path, fn) -> Path:
    data = bytearray(src.read_bytes())
    fn(data, ELF.from_bytes(bytes(data)))
    dst.write_bytes(bytes(data))
    return dst


def _phdr_off(e: ELF, i: int) -> int:
    phoff, = struct.unpack_from("<Q", e.data, 0x20)
    phentsize, = struct.unpack_from("<H", e.data, 0x36)
    return phoff + i * phentsize


@needs_cc
def test_a_good_link_validates_clean(lib_sources):
    for out in (_cc(lib_sources, "app", "main.c"),
                _cc(lib_sources, "libfoo.so", "-shared", "-fPIC", "foo.c")):
        with open_elf(out) as e:
            assert validate(e) == []


@needs_cc
def test_validate_finds_broken_images(lib_sources):
    exe = _cc(lib_sources, "app", "main.c")
    with open_elf(exe) as e:
        if not e.is64 or not e.le:
            pytest.skip("patches assume a 64-bit little-endian host")
        load = next(i for i, p in enumerate(e.phdrs) if p.type == PT_LOAD)
        text = next(s for s in e.sections if s.name == ".text")

    def entry_nowhere(d, e):
        struct.pack_into("<Q", d, 0x18, 0x7FFF0000)

    def odd_align(d, e):
        struct.pack_into("<Q", d, _phdr_off(e, load) + 48, 3)

    def overlap(d, e):
        i, j = [k for k, p in enumerate(e.phdrs) if p.type == PT_LOAD][:2]
        struct.pack_into("<Q", d, _phdr_off(e, j) + 16, e.phdrs[i].vaddr)

    def section_past_end(d, e):
        shoff, = struct.unpack_from("<Q", d, 0x28)
        idx = e.sections.index(text)
        struct.pack_into("<Q", d, shoff + idx * 64 + 24, len(d) + 0x1000)

    cases = {"entry": entry_nowhere, "segment": odd_align, "overlap": overlap, "section": section_past_end}
    for name, fn in cases.items():
        bad = _patch(exe, lib_sources / f"bad-{name}", fn)
        with open_elf(bad) as e:
            found = validate(e) if e else None
        assert found, name
        assert any(f.level == "error" for f in found), (name, found)


# ---------------------------------------------------------------------------- compare

@needs_cc
def test_same_link_twice_has_no_differences(lib_sources):
    a = _cc(lib_sources, "a.so", "-shared", "-fPIC", "foo.c", "-Wl,--version-script=foo.map")
    b = _cc(lib_sources, "b.so", "-shared", "-fPIC", "foo.c", "-Wl,--version-script=foo.map")
    with open_elf(a) as ra, open_elf(b) as rb:
        assert _significant(compare(ra, rb)) == []


@needs_cc
@pytest.mark.parametrize("ref_args,new_args,expect", [
    (["-Wl,-z,now"], ["-Wl,-z,lazy"], ("error", "bind-now")),
    (["-Wl,-z,relro"], ["-Wl,-z,norelro"], ("error", "relro")),
    (["-Wl,-z,noexecstack"], ["-Wl,-z,execstack"], ("error", "stack")),
    (["-Wl,-soname,libfoo.so.1"], ["-Wl,-soname,libfoo.so.2"], ("error", "soname")),
    (["-Wl,--version-script=foo.map"], [], ("error", "versions")),
    (["-Wl,--version-script=foo.map"], [], ("warn", "exports")),
    (["-Wl,--no-as-needed", "-lm"], ["-Wl,--as-needed", "-lm"], ("error", "needed")),
    (["-Wl,--as-needed", "-lm"], ["-Wl,--no-as-needed", "-lm"], ("warn", "needed")),
    (["-Wl,--build-id"], ["-Wl,--build-id=none"], ("warn", "build-id")),
])
def test_compare_reports_what_changed(lib_sources, ref_args, new_args, expect):
    ref = _cc(lib_sources, "ref.so", "-shared", "-fPIC", "foo.c", *ref_args)
    new = _cc(lib_sources, "new.so", "-shared", "-fPIC", "foo.c", *new_args)
    with open_elf(ref) as r, open_elf(new) as n:
        found = _significant(compare(r, n))
    assert expect in found, found


@needs_cc
def test_compare_pie_and_kind(lib_sources):
    pie = _cc(lib_sources, "pie", "-fPIE", "-pie", "main.c")
    nopie = _cc(lib_sources, "nopie", "-fno-PIE", "-no-pie", "main.c")
    so = _cc(lib_sources, "x.so", "-shared", "-fPIC", "foo.c")
    with open_elf(pie) as p, open_elf(nopie) as n, open_elf(so) as s:
        assert ("error", "pie") in _significant(compare(p, n)) or ("error", "kind") in _significant(compare(p, n))
        assert ("error", "interpreter") in _significant(compare(p, s)) or \
            ("error", "kind") in _significant(compare(p, s))


@needs_cc
def test_compare_ignores_tree_paths_in_runpath(tmp_path, lib_sources):
    a = _cc(lib_sources, "a", "main.c", f"-Wl,-rpath,{tmp_path}/ref/lib")
    b = _cc(lib_sources, "b", "main.c", f"-Wl,-rpath,{tmp_path}/eld/lib")
    with open_elf(a) as ra, open_elf(b) as rb:
        assert ("warn", "runpath") in _significant(compare(ra, rb))
        assert ("warn", "runpath") not in _significant(
            compare(ra, rb, (f"{tmp_path}/ref", f"{tmp_path}/eld")))


@needs_cc
def test_compare_command(lib_sources, capsys):
    a = _cc(lib_sources, "a.so", "-shared", "-fPIC", "foo.c", "-Wl,-z,now")
    b = _cc(lib_sources, "b.so", "-shared", "-fPIC", "foo.c", "-Wl,-z,lazy")
    assert main(["compare", str(a), str(a)]) == 0
    capsys.readouterr()
    assert main(["compare", str(a), str(b)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any(f["what"] == "bind-now" and f["check"] == "compare" for f in out["findings"])
    assert main(["compare", str(a), str(lib_sources / "foo.c")]) == 2


# ---------------------------------------------------------------------------- check --compare

LOSE_Z_NOW = """#!/bin/sh
case "$1" in --help) echo "  --gc-sections  x"; exit 0;; --version) echo "fake eld 1"; exit 0;; esac
n=$#; i=0; skip=0
while [ $i -lt $n ]; do a="$1"; shift; i=$((i+1))
  if [ $skip = 1 ]; then skip=0; [ "$a" = now ] && continue; set -- "$@" -z "$a"; continue; fi
  case "$a" in -z) skip=1; continue;; esac
  set -- "$@" "$a"
done
exec ld "$@"
"""


def _project(tmp_path, eld_script):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("LDFLAGS = -Wl,-z,now\nall: app\napp: main.c\n"
                                  "\t$(CC) -o $@ main.c $(LDFLAGS)\nclean:\n\trm -f app\n")
    (src / "main.c").write_text("int main(void){return 0;}\n")
    eld = tmp_path / "ld.eld"
    eld.write_text(eld_script)
    eld.chmod(0o755)
    dst = tmp_path / "dst"
    convert_project(src, dst, eld=str(eld))
    return src, dst, eld


@needs_cc
@pytest.mark.skipif(not shutil.which("make"), reason="needs make")
def test_check_compare_reports_lost_hardening(tmp_path):
    src, dst, eld = _project(tmp_path, LOSE_Z_NOW)
    res = run_check(dst, eld=str(eld), probe=False, compare=True)
    assert not res.ok
    assert any("BIND_NOW" in p for p in res.problems), res.problems
    assert res.reference["returncode"] == 0 and res.reference["compared"] == 1
    ref = Path(res.reference["dir"])
    assert ref == tmp_path / "dst.reference" and (ref / REFERENCE_MARK).is_file()
    assert (ref / "app").is_file() and Path(res.reference["build_log"]).is_file()
    [out] = [o for o in res.outputs if o.get("differences") is not None]
    assert out["reference"] == str(ref / "app")
    assert not list((src).glob("app")), "the source tree is never built in"


@needs_cc
@pytest.mark.skipif(not shutil.which("make"), reason="needs make")
def test_check_compare_passes_when_nothing_differs(tmp_path):
    src, dst, eld = _project(tmp_path, '#!/bin/sh\ncase "$1" in --help|--version) echo eld; exit 0;; esac\n'
                                       'exec ld "$@"\n')
    res = run_check(dst, eld=str(eld), probe=False, compare=True)
    assert res.ok, res.problems
    assert res.reference["compared"] == 1
    # run again: the previous reference build is replaced
    assert run_check(dst, eld=str(eld), probe=False, compare=True).ok


@needs_cc
@pytest.mark.skipif(not shutil.which("make"), reason="needs make")
def test_check_compare_never_replaces_a_foreign_directory(tmp_path):
    src, dst, eld = _project(tmp_path, '#!/bin/sh\nexec ld "$@"\n')
    mine = tmp_path / "dst.reference"
    mine.mkdir()
    (mine / "precious").write_text("x")
    res = run_check(dst, eld=str(eld), probe=False, compare=True)
    assert any("refusing to replace" in p for p in res.problems)
    assert (mine / "precious").read_text() == "x"


@needs_cc
@pytest.mark.skipif(not shutil.which("make"), reason="needs make")
def test_check_compare_needs_the_source_tree(tmp_path):
    src, dst, eld = _project(tmp_path, '#!/bin/sh\nexec ld "$@"\n')
    shutil.rmtree(src)
    res = run_check(dst, eld=str(eld), probe=False, compare=True)
    assert any("--compare: the source tree" in p for p in res.problems)


@needs_cc
@pytest.mark.skipif(not shutil.which("make"), reason="needs make")
def test_check_validates_every_output(tmp_path):
    """An eld that writes a broken image is caught even without --compare."""
    breaker = tmp_path / "break.py"
    breaker.write_text("import struct, sys\np = sys.argv[1]\nd = bytearray(open(p, 'rb').read())\n"
                       "struct.pack_into('<Q', d, 0x18, 0x7FFF0000)\nopen(p, 'wb').write(d)\n")
    src, dst, eld = _project(tmp_path, f"""#!/bin/sh
case "$1" in --help|--version) echo eld; exit 0;; esac
ld "$@" || exit
out=; prev=
for a; do [ "$prev" = -o ] && out=$a; prev=$a; done
[ -n "$out" ] && {os.sys.executable} {breaker} "$out"
exit 0
""")
    res = run_check(dst, eld=str(eld), probe=False)
    assert any("malformed ELF" in p and "entry point" in p for p in res.problems), res.problems
