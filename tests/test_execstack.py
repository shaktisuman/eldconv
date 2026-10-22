"""The shim's stack decision (execstack.py): GNU ld's rules for PT_GNU_STACK, from the inputs'
.note.GNU-stack sections, for an eld that writes the segment only when told to."""
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from eldconv import execstack
from eldconv.execstack import decide, elf_note
from eldconv.generator import SHIM_LD, STACK_HELPER, convert_project

CC = shutil.which("cc") or shutil.which("gcc")
AR = shutil.which("ar")
EM_X86_64, EM_AARCH64, EM_ARM, EM_RISCV = 62, 183, 40, 243


def rel(path: Path, machine=EM_X86_64, note=True, exec_note=False, is64=True, le=True) -> Path:
    """A minimal relocatable ELF object: a .text and, if `note`, a .note.GNU-stack."""
    e = "<" if le else ">"
    names = [b"", b".text", b".shstrtab"] + ([b".note.GNU-stack"] if note else [])
    strtab = b"\0".join(names) + b"\0"
    offs, o = [], 0
    for n in names:
        offs.append(o)
        o += len(n) + 1
    ehsize, shentsize = (64, 64) if is64 else (52, 40)
    shoff = ehsize + len(strtab)
    shoff += (-shoff) % 8
    sections = [(0, 0, 0, 0, 0), (offs[1], 1, 6, 0, 0), (offs[2], 3, 0, ehsize, len(strtab))]
    if note:
        sections.append((offs[3], 1, 4 if exec_note else 0, 0, 0))
    hdr = bytearray(b"\x7fELF" + bytes([2 if is64 else 1, 1 if le else 2, 1]) + b"\0" * 9)
    if is64:
        hdr += struct.pack(e + "HHIQQQIHHHHHH", 1, machine, 1, 0, 0, shoff, 0, ehsize, 0, 0,
                           shentsize, len(sections), 2)
    else:
        hdr += struct.pack(e + "HHIIIIIHHHHHH", 1, machine, 1, 0, 0, shoff, 0, ehsize, 0, 0,
                           shentsize, len(sections), 2)
    data = bytes(hdr) + strtab
    data += b"\0" * (shoff - len(data))
    for name, typ, flags, off, size in sections:
        if is64:
            data += struct.pack(e + "IIQQQQIIQQ", name, typ, flags, 0, off, size, 0, 0, 1, 0)
        else:
            data += struct.pack(e + "IIIIIIIIII", name, typ, flags, 0, off, size, 0, 0, 1, 0)
    path.write_bytes(data)
    return path


def ar(path: Path, members, thin=False) -> Path:
    """A GNU ar archive (or a thin one) of `members`, without a symbol table."""
    data = bytearray(b"!<thin>\n" if thin else b"!<arch>\n")
    for m in members:
        body = m.read_bytes()
        data += ((m.name + "/").ljust(16) + "0".ljust(12) + "0".ljust(6) * 2 + "644".ljust(8)
                 + str(len(body)).ljust(10) + "`\n").encode()
        if not thin:
            data += body + (b"\n" if len(body) & 1 else b"")
    path.write_bytes(bytes(data))
    return path


def run(args, cwd):
    warnings = []
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return decide(args, warn=warnings.append), warnings
    finally:
        os.chdir(old)


@pytest.mark.parametrize("is64", [True, False])
@pytest.mark.parametrize("le", [True, False])
def test_reads_the_note_in_every_elf_flavour(tmp_path, is64, le):
    assert elf_note(rel(tmp_path / "a.o", is64=is64, le=le).read_bytes()) == (EM_X86_64, True, False)
    assert elf_note(rel(tmp_path / "b.o", exec_note=True, is64=is64, le=le).read_bytes())[2] is True
    assert elf_note(rel(tmp_path / "c.o", note=False, is64=is64, le=le).read_bytes())[1] is False


def test_what_is_not_a_relocatable_object(tmp_path):
    data = bytearray(rel(tmp_path / "a.o").read_bytes())
    struct.pack_into("<H", data, 16, 3)              # ET_DYN: a shared library
    assert elf_note(bytes(data)) is None
    assert elf_note(b"BC\xc0\xde" + b"\0" * 60) is None          # LLVM bitcode
    assert elf_note(b"/* GNU ld script */\nGROUP(libc.so.6)\n") is None
    assert elf_note(rel(tmp_path / "b.o").read_bytes()[:40]) is None


def test_every_input_with_the_note(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "b.o")
    assert run(["-o", "out", "a.o", "b.o"], tmp_path) == ("-znoexecstack", [])


def test_an_executable_note_wins(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "x.o", exec_note=True)
    z, w = run(["a.o", "x.o"], tmp_path)
    assert z == "-zexecstack" and "x.o: requires executable stack" in w[0]


def test_a_missing_note_implies_an_executable_stack_where_gnu_ld_says_so(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "asm.o", note=False)
    z, w = run(["a.o", "asm.o"], tmp_path)
    assert z == "-zexecstack"
    assert "asm.o: missing .note.GNU-stack section implies executable stack" in w[0] and "NOTE" in w[1]
    rel(tmp_path / "arm.o", machine=EM_ARM)
    rel(tmp_path / "arm-asm.o", machine=EM_ARM, note=False)
    assert run(["arm.o", "arm-asm.o"], tmp_path)[0] == "-zexecstack"


@pytest.mark.parametrize("machine", [EM_AARCH64, EM_RISCV])
def test_not_on_aarch64_and_riscv(tmp_path, machine):
    """Their GNU ld backends set elf_backend_default_execstack to 0."""
    rel(tmp_path / "a.o", machine=machine)
    rel(tmp_path / "asm.o", machine=machine, note=False)
    assert run(["a.o", "asm.o"], tmp_path) == ("-znoexecstack", [])


def test_no_note_anywhere_leaves_it_to_the_loader(tmp_path):
    """GNU ld writes no PT_GNU_STACK when no input has the note."""
    rel(tmp_path / "asm.o", note=False)
    assert run(["asm.o"], tmp_path) == ("", [])
    assert run(["asm.o", "-z", "stack-size=65536"], tmp_path)[0] == "-zexecstack"


def test_the_command_line_decides_first(tmp_path):
    rel(tmp_path / "x.o", exec_note=True)
    for args in (["-z", "noexecstack"], ["-znoexecstack"], ["-z", "execstack"], ["-r"], ["--relocatable"]):
        assert run(["x.o", *args], tmp_path) == ("", []), args


def test_warnings_can_be_silenced(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "asm.o", note=False)
    assert run(["--no-warn-execstack", "a.o", "asm.o"], tmp_path) == ("-zexecstack", [])


def test_archives_count_only_under_whole_archive(tmp_path):
    rel(tmp_path / "a.o")
    members = [rel(tmp_path / "m1.o"), rel(tmp_path / "m2.o", note=False)]
    ar(tmp_path / "libm.a", members)
    assert run(["a.o", "libm.a"], tmp_path)[0] == "-znoexecstack"
    z, w = run(["a.o", "--whole-archive", "libm.a", "--no-whole-archive"], tmp_path)
    assert z == "-zexecstack" and "libm.a(m2.o)" in w[0]
    assert run(["a.o", "--whole-archive", "--no-whole-archive", "libm.a"], tmp_path)[0] == "-znoexecstack"


def test_thin_archive_members_are_read_from_disk(tmp_path):
    rel(tmp_path / "a.o")
    ar(tmp_path / "libt.a", [rel(tmp_path / "t.o", exec_note=True)], thin=True)
    assert run(["a.o", "--whole-archive", "libt.a"], tmp_path)[0] == "-zexecstack"


def test_response_files_and_just_symbols(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "x.o", exec_note=True)
    (tmp_path / "link.rsp").write_text("-o\nout\na.o\n")
    assert run(["@link.rsp"], tmp_path)[0] == "-znoexecstack"
    (tmp_path / "z.rsp").write_text("-z\nnoexecstack\n")
    assert run(["a.o", "x.o", "@z.rsp"], tmp_path)[0] == ""
    assert run(["a.o", "-R", "x.o"], tmp_path)[0] == "-znoexecstack"
    assert run(["a.o", "--just-symbols", "x.o"], tmp_path)[0] == "-znoexecstack"


@pytest.mark.skipif(not (CC and AR), reason="needs cc and ar")
def test_real_objects_from_the_host_toolchain(tmp_path):
    (tmp_path / "c.c").write_text("int f(void){return 1;}\n")
    (tmp_path / "plain.s").write_text(".text\n.globl g\ng: ret\n")
    (tmp_path / "exec.s").write_text('.text\n.globl h\nh: ret\n.section .note.GNU-stack,"x",@progbits\n')
    for src in ("c.c", "plain.s", "exec.s"):
        subprocess.run([CC, "-c", src], cwd=tmp_path, check=True, capture_output=True)
    assert elf_note((tmp_path / "c.o").read_bytes())[1:] == (True, False)
    assert elf_note((tmp_path / "plain.o").read_bytes())[1] is False
    assert elf_note((tmp_path / "exec.o").read_bytes())[1:] == (True, True)
    subprocess.run([AR, "rcs", "libx.a", "c.o", "plain.o"], cwd=tmp_path, check=True)
    assert run(["c.o", "libx.a"], tmp_path)[0] == "-znoexecstack"
    machine = elf_note((tmp_path / "c.o").read_bytes())[0]
    want = "-znoexecstack" if machine in execstack.NO_DEFAULT_EXECSTACK else "-zexecstack"
    assert run(["c.o", "--whole-archive", "libx.a"], tmp_path)[0] == want


# --------------------------------------------------------------------------------- shim

def _shim(tmp_path, helper=True):
    eld = tmp_path / "echo-eld"
    eld.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    eld.chmod(0o755)
    d = tmp_path / "eld-bin"
    d.mkdir()
    shim = d / "ld"
    shim.write_text(SHIM_LD.replace("@ELD@", str(eld)).replace("@SHIM_DIR@", str(d))
                    .replace("@PYTHON@", shutil.which("python3") or "python3"))
    shim.chmod(0o755)
    if helper:
        (d / STACK_HELPER).write_text(Path(execstack.__file__).read_text())
    return shim


def _link(shim, args, cwd, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("ELDCONV_")}
    e.update(env)
    r = subprocess.run([str(shim), *args], capture_output=True, text=True, env=e, cwd=cwd, check=True)
    return r.stdout.splitlines(), r.stderr


def test_shim_passes_eld_the_decision(tmp_path):
    rel(tmp_path / "a.o")
    rel(tmp_path / "asm.o", note=False)
    shim = _shim(tmp_path)
    assert _link(shim, ["-o", "out", "a.o"], tmp_path)[0] == ["-o", "out", "a.o", "-znoexecstack"]
    out, err = _link(shim, ["a.o", "asm.o"], tmp_path)
    assert out[-1] == "-zexecstack" and "missing .note.GNU-stack" in err
    assert _link(shim, ["a.o", "-z", "execstack"], tmp_path)[0] == ["a.o", "-z", "execstack"]
    assert _link(shim, ["a.o"], tmp_path, ELDCONV_KEEP_STACK="1")[0] == ["a.o"]
    assert _link(shim, ["--version"], tmp_path)[0] == ["--version"]


def test_shim_without_the_helper_or_python_links_anyway(tmp_path):
    rel(tmp_path / "a.o")
    assert _link(_shim(tmp_path, helper=False), ["a.o"], tmp_path) == (["a.o"], "")
    shim = tmp_path / "eld-bin" / "ld"
    shim.write_text(shim.read_text().replace(shutil.which("python3") or "python3", "/nonexistent/python3"))
    (tmp_path / "eld-bin" / STACK_HELPER).write_text(Path(execstack.__file__).read_text())
    out, err = _link(shim, ["a.o"], tmp_path, PATH="/nonexistent")
    assert out == ["a.o"] and "no python3" in err


def test_conversion_installs_the_helper(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Makefile").write_text("all:\n\t$(CC) -o app main.c\n")
    dst = tmp_path / "dst"
    convert_project(src, dst, eld="/bin/true")
    shim = (dst / "eld-bin" / "ld").read_text()
    assert (dst / "eld-bin" / STACK_HELPER).is_file() and "@PYTHON@" not in shim and "@SHIM_DIR@" not in shim
