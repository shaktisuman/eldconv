"""End-to-end: convert a real project and build it through the shim.

Uses a fake `ld.eld` that forwards to the system ld (or the real ld.eld if
ELDCONV_TEST_ELD is set), so the whole pipeline is exercised without eld.
"""
import os
import shutil
from pathlib import Path

import pytest

from eldconv.checker import run_check
from eldconv.generator import convert_project

FIX = Path(__file__).parent / "fixtures"
HAVE_CC = shutil.which("cc") or shutil.which("gcc")
HAVE_MAKE = shutil.which("make")
HAVE_CMAKE = shutil.which("cmake")
HAVE_LD = shutil.which("ld")


@pytest.fixture
def eld(tmp_path):
    real = os.environ.get("ELDCONV_TEST_ELD")
    if real:
        return real
    if not HAVE_LD:
        pytest.skip("no system ld")
    p = tmp_path / "fake-eld" / "ld.eld"
    p.parent.mkdir()
    # emulate eld: reject flags eld would reject, expose a --help, else forward to ld
    p.write_text(f"""#!/bin/sh
case "$1" in
  --help) printf '%s\n' "OPTIONS:" "  --gc-sections  gc" "  -Map=<file>  map" "  -z <opt>  z" "  --as-needed  x" "  --hash-style=<s>  h" "  -shared  s" "  --thinlto-jobs=<n>  j"; exit 0;;
  --version) echo "fake eld 0.0"; exit 0;;
esac
for a in "$@"; do case "$a" in --icf=*|--fork|--perf|--pack-dyn-relocs=*|--compress-debug-sections=*) echo "eld: unknown option $a" >&2; exit 1;; esac; done
exec {HAVE_LD} "$@"
""")
    p.chmod(0o755)
    return str(p)


def _strip_unsupported_by_system_ld(dst: Path):
    # gold fixture uses -Wl,--incremental-full (dropped) etc.; nothing else to do.
    pass


@pytest.mark.skipif(not (HAVE_CC and HAVE_MAKE), reason="needs cc + make")
def test_e2e_make_gold(tmp_path, eld):
    dst = tmp_path / "out"
    rep = convert_project(FIX / "make_gold", dst, eld=eld)
    assert rep.build["linker"] == "gold"
    res = run_check(dst, jobs=2)
    assert res.ok, (res.problems, res.stderr_tail)
    assert res.invocations >= 1
    assert all(o["elf"] for o in res.outputs)
    assert (dst / "app").exists()
    # the shim must have been the linker: it is the only thing that writes the log
    assert (Path(res.build_dir) / "eld-link.log").read_text().count("\n") == res.invocations


@pytest.mark.skipif(not (HAVE_CC and HAVE_MAKE), reason="needs cc + make")
def test_e2e_make_mold(tmp_path, eld):
    src = tmp_path / "src"
    shutil.copytree(FIX / "make_mold", src)
    # fixture pins clang; use whatever cc exists here
    (src / "Makefile").write_text((src / "Makefile").read_text().replace("CC = clang", "CC = cc"))
    dst = tmp_path / "out"
    convert_project(src, dst, eld=eld, source_linker="mold")
    mk = (dst / "Makefile").read_text()
    assert "--fork" not in mk and "mold -run" not in mk and "-Wl,-z,now" in mk
    res = run_check(dst, jobs=2)
    assert res.ok, (res.problems, res.stderr_tail)


@pytest.mark.skipif(not (HAVE_CC and HAVE_MAKE), reason="needs cc + make")
def test_e2e_unconverted_flag_is_reported(tmp_path, eld):
    """A flag eld does not know must be reported even though eld links anyway.

    Real eld warns and exits 0, so the build "succeeds" with the flag silently
    doing nothing -- exactly the failure a checker exists to catch.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.c").write_text("int main(void){return 0;}\n")
    # --icf classified as unknown for bfd -> kept -> fake eld rejects -> build fails
    (src / "Makefile").write_text("LDFLAGS = -Wl,--icf=all\napp: main.c\n\t$(CC) $(LDFLAGS) -o $@ $<\n")
    dst = tmp_path / "out"
    convert_project(src, dst, eld=eld, source_linker="bfd")
    res = run_check(dst, jobs=1)
    assert not res.ok
    assert any("build failed" in p for p in res.problems) or \
           any("silently ignored" in p for p in res.problems)


@pytest.mark.skipif(not (HAVE_CC and HAVE_CMAKE), reason="needs cc + cmake")
def test_e2e_cmake(tmp_path, eld):
    src = tmp_path / "src"
    shutil.copytree(FIX / "cmake_lld", src)
    txt = (src / "CMakeLists.txt").read_text().replace("set(CMAKE_C_COMPILER clang)\n", "")
    (src / "CMakeLists.txt").write_text(txt)
    dst = tmp_path / "out"
    convert_project(src, dst, eld=eld, source_linker="lld")
    res = run_check(dst, jobs=2)
    assert res.ok, (res.problems, res.stderr_tail)
    assert res.invocations == 2  # exe + shared lib, try_compile and probes excluded
    assert res.probes > 0 and res.trycompiles > 0
    links = [o for o in res.outputs if not o.get("trycompile")]
    assert all(o["elf"] for o in links)
    assert {o["elf_type"] for o in links} == {"DYN"}  # pie exe + shared lib
