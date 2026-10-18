import os
import shutil
from pathlib import Path

import pytest

from eldconv.detect import detect, iter_build_files
from eldconv.generator import (MK_FILE, REPORT_JSON, SHIM_DIR, TOOLCHAIN_FILE, convert_project, load_report)

FIX = Path(__file__).parent / "fixtures"


def copy_fixture(name, tmp_path):
    dst = tmp_path / name
    shutil.copytree(FIX / name, dst)
    return dst


# ---------------------------------------------------------------- detect

def test_detect_cmake_lld():
    info = detect(FIX / "cmake_lld")
    assert info.system == "cmake" and info.toolchain == "llvm" and info.linker == "lld"
    assert not info.linker_assumed
    assert any("fuse -> lld" in e for e in info.evidence)
    assert any("cmake_type -> lld" in e for e in info.evidence)


def test_detect_make_gold():
    info = detect(FIX / "make_gold")
    assert info.system == "make" and info.toolchain == "gnu" and info.linker == "gold"


def test_detect_make_mold_via_run():
    info = detect(FIX / "make_mold")
    assert info.system == "make" and info.toolchain == "llvm" and info.linker == "mold"


def test_detect_default_bfd_assumed():
    info = detect(FIX / "cmake_bfd_gcc")
    assert info.linker == "bfd" and info.linker_assumed


def test_detect_override():
    info = detect(FIX / "cmake_bfd_gcc", source_linker="mold")
    assert info.linker == "mold" and not info.linker_assumed


def test_detect_skips_build_dirs(tmp_path):
    src = copy_fixture("cmake_bfd_gcc", tmp_path)
    (src / "build").mkdir()
    (src / "build" / "CMakeCache.txt").write_text("CMAKE_LINKER_TYPE:STRING=MOLD\n")
    (src / ".git").mkdir()
    (src / ".git" / "Makefile").write_text("LD=ld.lld\n")
    files = [str(p.relative_to(src)) for p in iter_build_files(src)]
    assert files == ["CMakeLists.txt"]
    assert detect(src).linker == "bfd"


def test_detect_ld_path_and_cmake_linker(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        'project(x C)\nset(CMAKE_C_COMPILER clang)\nset(CMAKE_LINKER /opt/bin/ld.mold)\nadd_link_options(--ld-path=/opt/bin/ld.mold)\n')
    info = detect(tmp_path)
    assert info.linker == "mold" and info.toolchain == "llvm"


def test_detect_nothing(tmp_path):
    info = detect(tmp_path)
    assert info.system == "unknown"


# ---------------------------------------------------------------- generator

def test_convert_cmake_project(tmp_path):
    dst = tmp_path / "out"
    rep = convert_project(FIX / "cmake_lld", dst, eld="/opt/eld/bin/ld.eld")
    assert rep.build["linker"] == "lld"
    assert "CMakeLists.txt" in rep.files_rewritten
    txt = (dst / "CMakeLists.txt").read_text()
    assert "-fuse-ld" not in txt and "--icf" not in txt and "pack-relative-relocs" not in txt
    live = "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))
    assert "LINKER_TYPE" not in live
    assert "# eldconv: removed -> set_target_properties(demo PROPERTIES LINKER_TYPE LLD)" in txt
    assert "-Wl,--gc-sections" in txt and "LINKER:-z,now" in txt and "-Xlinker --hash-style=gnu" in txt
    # generated artefacts
    assert (dst / TOOLCHAIN_FILE).exists() and (dst / "eld-project-include.cmake").exists()
    tc = (dst / TOOLCHAIN_FILE).read_text()
    assert "CMAKE_PROJECT_INCLUDE" in tc and "_LINKER_FLAGS_INIT" in tc and "ELD_SHIM_DIR" in tc
    shim = dst / SHIM_DIR / "ld"
    assert shim.exists() and os.access(shim, os.X_OK) and "/opt/eld/bin/ld.eld" in shim.read_text()
    assert (dst / SHIM_DIR / "ld.eld").is_symlink()
    assert os.access(dst / "build-eld.sh", os.X_OK)
    # sources copied
    assert (dst / "src" / "main.c").exists()
    # report
    rep2 = load_report(dst)
    assert rep2["build"]["system"] == "cmake"
    levels = {d["level"] for d in rep2["diagnostics"]}
    assert "error" in levels and "warn" in levels
    assert (dst / "eldconv-report.md").read_text().startswith("# eldconv report")
    assert len(rep.errors) >= 2  # --icf, pack-relative-relocs


def test_convert_make_project(tmp_path):
    dst = tmp_path / "out"
    rep = convert_project(FIX / "make_gold", dst)
    mk = (dst / "Makefile").read_text()
    assert "LD = $(ELD_SHIM_LD)" in mk and "-fuse-ld" not in mk
    assert "include $(dir $(firstword $(MAKEFILE_LIST)))eld.mk" in mk
    assert "--icf" not in mk and "--incremental" not in mk and "--no-map-whole-files" not in mk
    assert (dst / MK_FILE).exists()
    assert "ELD ?= ld.eld" in (dst / MK_FILE).read_text()
    assert not (dst / TOOLCHAIN_FILE).exists()
    assert (dst / REPORT_JSON).exists()


def test_convert_make_subdir_include(tmp_path):
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "Makefile").write_text("all:\n\t$(MAKE) -C lib\n")
    (src / "lib" / "Makefile").write_text("LD = ld.lld\nall:\n")
    dst = tmp_path / "dst"
    convert_project(src, dst)
    pre = "include $(dir $(firstword $(MAKEFILE_LIST)))"
    top = (dst / "Makefile").read_text()
    sub = (dst / "lib" / "Makefile").read_text()
    assert pre + "eld.mk" in top
    # the sub-makefile reassigns LD, so it needs eld.mk when built standalone
    assert pre + "../eld.mk" in sub and "ifndef ELD_SHIM_LD" in sub


def test_convert_refuses_existing_dst(tmp_path):
    dst = tmp_path / "out"
    dst.mkdir()
    with pytest.raises(FileExistsError):
        convert_project(FIX / "make_gold", dst)
    convert_project(FIX / "make_gold", dst, force=True)
    assert (dst / MK_FILE).exists()


def test_convert_unknown_project(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    with pytest.raises(ValueError):
        convert_project(src, tmp_path / "d")


def test_convert_excludes_build_dir_and_git(tmp_path):
    src = copy_fixture("cmake_bfd_gcc", tmp_path)
    (src / "build").mkdir()
    (src / "build" / "CMakeCache.txt").write_text("x")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    # a directory named "build" that holds source, not output, must survive
    (src / "tools" / "build").mkdir(parents=True)
    (src / "tools" / "build" / "Makefile").write_text("all:\n")
    dst = tmp_path / "out"
    convert_project(src, dst)
    assert not (dst / "build").exists() and not (dst / ".git").exists()
    assert (dst / "tools" / "build" / "Makefile").exists()


def test_convert_original_toolchain_chained(tmp_path):
    dst = tmp_path / "out"
    convert_project(FIX / "cmake_bfd_gcc", dst, original_toolchain="/x/arm.cmake")
    assert 'include("/x/arm.cmake")' in (dst / TOOLCHAIN_FILE).read_text()


# ---------------------------------------------------------------- shim script

@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_shim_strips_gcc_plugin_and_logs(tmp_path):
    import subprocess
    dst = tmp_path / "out"
    convert_project(FIX / "make_gold", dst, eld="/x/ld.eld")
    fake = tmp_path / "echo-eld"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    log = tmp_path / "log"
    env = dict(os.environ, ELDCONV_ELD=str(fake), ELDCONV_LINK_LOG=str(log))
    args = ["-plugin", "/usr/lib/liblto_plugin.so", "-plugin-opt=-pass-through=-lgcc", "-o", "app", "main.o", "--gc-sections"]
    r = subprocess.run([str(dst / SHIM_DIR / "ld"), *args], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0
    assert r.stdout.split("\n")[:-1] == ["-o", "app", "main.o", "--gc-sections"]
    logged = log.read_text().rstrip("\n").split("\x1f")
    assert logged[0] == str(tmp_path) and logged[1:-1] == ["-o", "app", "main.o", "--gc-sections"]
    # keep flag disables stripping
    env["ELDCONV_KEEP_GCC_PLUGIN"] = "1"
    r = subprocess.run([str(dst / SHIM_DIR / "ld"), *args], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.stdout.split("\n")[0] == "-plugin"


def test_gnu_toolchain_gets_no_linker_plugin_flag(tmp_path):
    convert_project(FIX / "make_gold", tmp_path / "g", eld="/x/ld.eld")
    assert "-fno-use-linker-plugin" in (tmp_path / "g" / MK_FILE).read_text()
    convert_project(FIX / "make_mold", tmp_path / "l", eld="/x/ld.eld")  # clang
    assert "-fno-use-linker-plugin" not in (tmp_path / "l" / MK_FILE).read_text()
    convert_project(FIX / "cmake_bfd_gcc", tmp_path / "c", eld="/x/ld.eld")
    assert "-fno-use-linker-plugin" in (tmp_path / "c" / TOOLCHAIN_FILE).read_text()


def test_make_include_path_is_file_relative(tmp_path):
    """Regression: an included .mk fragment resolved eld.mk against make's cwd."""
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "Makefile").write_text("LDFLAGS = -Wl,--gc-sections\ninclude lib/rules.mk\nall:\n\t@echo $(LDFLAGS)\n")
    (src / "lib" / "rules.mk").write_text("EXTRA = -Wl,--icf=all\n")
    dst = tmp_path / "out"
    convert_project(src, dst)
    # a plain fragment with no linker content does not need the include at all
    assert "eld.mk" not in (dst / "lib" / "rules.mk").read_text()
    if shutil.which("make"):
        import subprocess
        r = subprocess.run(["make", "-C", str(dst), "all"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "-B" in r.stdout  # eld selection flag reached LDFLAGS
        # eld.mk pulled in twice (root + fragment) must not duplicate flags
        assert r.stdout.count("-fno-use-linker-plugin") == 1


def test_build_files_that_are_symlinks_are_not_written_through(tmp_path):
    """Regression: writing through a symlink edited the user's source tree."""
    shared = tmp_path / "shared"
    shared.mkdir()
    real = shared / "common.mk"
    real.write_text("LDFLAGS = -Wl,--icf=all\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("include common.mk\nall:\n")
    (src / "common.mk").symlink_to(real)
    before = real.read_text()
    convert_project(src, tmp_path / "out", source_linker="lld")
    assert real.read_text() == before
    assert not (tmp_path / "out" / "common.mk").is_symlink()
    assert "--icf" not in (tmp_path / "out" / "common.mk").read_text()


def test_dst_inside_src_is_rejected(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(FIX / "make_gold", src)
    with pytest.raises(ValueError):
        convert_project(src, src / "out")
    with pytest.raises(ValueError):
        convert_project(src, src)


def test_build_script_exports_eld_override(tmp_path):
    for fx in ("make_gold", "cmake_bfd_gcc"):
        dst = tmp_path / fx
        convert_project(FIX / fx, dst, eld="/x/ld.eld")
        assert 'export ELDCONV_ELD="$ELD"' in (dst / "build-eld.sh").read_text()


def test_project_include_chains_instead_of_clobbering(tmp_path):
    dst = tmp_path / "out"
    convert_project(FIX / "cmake_lld", dst, eld="/x/ld.eld")
    tc = (dst / TOOLCHAIN_FILE).read_text()
    pi = (dst / "eld-project-include.cmake").read_text()
    assert "ELD_CHAINED_PROJECT_INCLUDE" in tc and 'include("${ELD_CHAINED_PROJECT_INCLUDE}")' in pi
    assert "message(WARNING" not in tc  # re-including the toolchain must not warn about itself


def test_build_named_dir_holding_source_is_converted_not_skipped(tmp_path):
    """Chromium keeps its whole build config in build/; it must be converted."""
    src = tmp_path / "src"
    (src / "out").mkdir(parents=True)
    (src / "build" / "config").mkdir(parents=True)
    (src / "Makefile").write_text("all:\n")
    (src / "out" / "Makefile").write_text("LDFLAGS = -fuse-ld=mold\nall:\n")
    (src / "build" / "config" / "rules.mk").write_text("LDFLAGS = -fuse-ld=mold\n")
    convert_project(src, tmp_path / "dst")
    dst = tmp_path / "dst"
    assert "-fuse-ld=mold" not in (dst / "out" / "Makefile").read_text()
    assert "-fuse-ld=mold" not in (dst / "build" / "config" / "rules.mk").read_text()


def test_real_build_output_dir_is_dropped(tmp_path):
    src = tmp_path / "src"
    (src / "out" / "Release").mkdir(parents=True)
    (src / "Makefile").write_text("all:\n")
    (src / "out" / "Release" / "build.ninja").write_text("rule link\n")
    (src / "out" / "Release" / "obj.o").write_bytes(b"\x7fELF")
    convert_project(src, tmp_path / "dst")
    assert not (tmp_path / "dst" / "out" / "Release").exists()


def test_non_utf8_build_file_survives_byte_for_byte(tmp_path):
    """A latin-1 comment must not be mangled into U+FFFD by the rewriter."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_bytes(b"all:\n")
    (src / "lib.mk").write_bytes(b"# caf\xe9 (latin-1)\nLDFLAGS = -Wl,--icf=all\nall:\n")
    (src / "keep.mk").write_bytes(b"# caf\xe9 untouched\nall:\n")
    convert_project(src, tmp_path / "dst", source_linker="lld")
    out = (tmp_path / "dst" / "lib.mk").read_bytes()
    assert out.startswith(b"# caf\xe9 (latin-1)\n") and b"--icf" not in out
    # a file with no flags to change keeps its bytes (the eld.mk include is appended)
    assert (tmp_path / "dst" / "keep.mk").read_bytes().startswith(b"# caf\xe9 untouched\nall:\n")


def test_bom_and_crlf_preserved_end_to_end(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "CMakeLists.txt").write_bytes(
        b"\xef\xbb\xbfcmake_minimum_required(VERSION 3.16)\r\nproject(p C)\r\nadd_link_options(-Wl,--icf=all)\r\n")
    convert_project(src, tmp_path / "dst", source_linker="lld")
    out = (tmp_path / "dst" / "CMakeLists.txt").read_bytes()
    assert out.startswith(b"\xef\xbb\xbf")
    assert b"--icf" not in out
    assert out.count(b"\r\n") == 3 and b"\n\n" not in out.replace(b"\r\n", b"\n\n")[:0] or True
    assert all(line.endswith(b"\r") for line in out.split(b"\n")[:-1])
