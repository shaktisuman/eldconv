"""Decide the stack a link gets, as GNU ld does, for a linker that does not.

GNU ld derives PT_GNU_STACK from the inputs' `.note.GNU-stack` sections (bfd/elflink.c,
bfd_elf_size_dynamic_sections). eld does not: it writes PT_GNU_STACK only for an explicit
`-z execstack` or `-z noexecstack`, and without the segment the loader falls back to its
default -- an executable stack on x86, ARM and others, which glibc 2.41 and later refuse to
dlopen. eldconv's shim runs this script on each link that names neither option, and passes
eld what it prints:

- `-z execstack` or `-z noexecstack` on the command line: nothing, the user decided;
- an input object whose `.note.GNU-stack` is executable: `-zexecstack`;
- an input object without the note, on a target whose GNU ld defaults to an executable stack
  (all but AArch64, RISC-V, LoongArch, IA-64 and KVX): `-zexecstack`;
- otherwise, when at least one input carries the note: `-znoexecstack`;
- no input carries the note: nothing (GNU ld writes no PT_GNU_STACK then either).

GNU ld's warnings for the first two cases are printed too (`--no-warn-execstack` silences
them). The inputs are the relocatable objects on the command line and in response files, and
every member of an archive under `--whole-archive`. Members another archive contributes are
not known before the link, so they are not read: GNU ld reads the ones it extracts.

This file runs on its own, under any Python 3 the build machine has: the shim copies it next
to itself, and it imports nothing from eldconv.
"""
import os
import shlex
import struct
import sys

# EM_* of the targets whose GNU ld backend sets elf_backend_default_execstack to 0
NO_DEFAULT_EXECSTACK = {183, 243, 258, 50, 256}   # AArch64, RISC-V, LoongArch, IA-64, KVX
EXPLICIT = ("execstack", "noexecstack")
RELOCATABLE = ("-r", "--relocatable", "-relocatable", "-i", "-Ur")
SHF_EXECINSTR = 4


def elf_note(data):
    """(e_machine, has .note.GNU-stack, note is executable) for a relocatable ELF object,
    or None for anything else (a shared library, a script, LLVM bitcode, a truncated file)."""
    if len(data) < 52 or data[:4] != b"\x7fELF" or data[4] not in (1, 2) or data[5] not in (1, 2):
        return None
    is64, end = data[4] == 2, "<" if data[5] == 1 else ">"
    e_type, machine = struct.unpack_from(end + "HH", data, 16)
    if e_type != 1:
        return None
    try:
        if is64:
            shoff, = struct.unpack_from(end + "Q", data, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", data, 0x3A)
        else:
            shoff, = struct.unpack_from(end + "I", data, 0x20)
            shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", data, 0x2E)
        if not shoff or shentsize < (64 if is64 else 40):
            return machine, False, False

        def header(i):
            o = shoff + i * shentsize
            if is64:
                name, _type, flags = struct.unpack_from(end + "IIQ", data, o)
                off, size = struct.unpack_from(end + "QQ", data, o + 24)
                link, = struct.unpack_from(end + "I", data, o + 40)
            else:
                name, _type, flags = struct.unpack_from(end + "III", data, o)
                off, size, link = struct.unpack_from(end + "III", data, o + 16)
            return name, flags, off, size, link
        if shnum == 0:                      # more than 0xff00 sections: the count is in [0]
            shnum = header(0)[3]
        if shstrndx == 0xFFFF:
            shstrndx = header(0)[4]
        if shnum > 1 << 20 or shstrndx >= shnum:
            return machine, False, False
        _, _, stroff, strsize, _ = header(shstrndx)
        want = b".note.GNU-stack\0"
        for i in range(shnum):
            name, flags, _, _, _ = header(i)
            if name < strsize and data[stroff + name:stroff + name + len(want)] == want:
                return machine, True, bool(flags & SHF_EXECINSTR)
        return machine, False, False
    except struct.error:
        return None


def archive_members(path, data):
    """(name, bytes) of each member of an ar archive; a thin archive's are read from disk."""
    thin = data[:8] == b"!<thin>\n"
    pos, names = 8, b""
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        try:
            size = int(hdr[48:58].decode().strip() or 0)
        except ValueError:
            return
        name = hdr[:16].decode(errors="replace").rstrip()
        body = pos + 60
        stored = not thin or name in ("/", "//", "/SYM64/") or name.startswith("#1/")
        if name == "//":
            names = data[body:body + size]
        elif name not in ("/", "/SYM64/", "__.SYMDEF", "__.SYMDEF SORTED"):
            if name.startswith("#1/"):                      # BSD: the name precedes the data
                n = int(name[3:] or 0)
                yield data[body:body + n].rstrip(b"\0").decode(errors="replace"), data[body + n:body + size]
            else:
                if name.startswith("/") and name[1:].isdigit():
                    off = int(name[1:])
                    name = names[off:names.find(b"\n", off)].decode(errors="replace")
                name = name.rstrip("/")
                if thin:
                    member = os.path.join(os.path.dirname(path), name)
                    try:
                        with open(member, "rb") as f:
                            yield name, f.read()
                    except OSError:
                        pass
                else:
                    yield name, data[body:body + size]
        pos = body + (size if stored else 0)
        pos += pos & 1


def expand(args, depth=0):
    """The arguments with each `@file` replaced by what it holds, as the linker reads it."""
    out = []
    for a in args:
        if a.startswith("@") and len(a) > 1 and depth < 8 and os.path.isfile(a[1:]):
            try:
                with open(a[1:], errors="replace") as f:
                    text = f.read()
                try:
                    inner = shlex.split(text)
                except ValueError:
                    inner = text.split()
                out.extend(expand(inner, depth + 1))
                continue
            except OSError:
                pass
        out.append(a)
    return out


def decide(argv, warn=print):
    """`-zexecstack`, `-znoexecstack` or "" for this link's arguments."""
    args = expand(argv)
    whole, warnings, stack_size = False, True, False
    inputs = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "-z" and i + 1 < len(args):
            z = args[i + 1]
            skip = True
        elif a.startswith("-z"):
            z = a[2:]
        else:
            z = None
        if z is not None:
            if z in EXPLICIT:
                return ""
            if z.startswith("stack-size="):
                stack_size = True
            continue
        if a in RELOCATABLE:
            return ""                       # -r keeps the notes for the final link to read
        if a in ("--whole-archive", "-whole-archive"):
            whole = True
        elif a in ("--no-whole-archive", "-no-whole-archive"):
            whole = False
        elif a in ("--no-warn-execstack",):
            warnings = False
        elif a in ("--warn-execstack",):
            warnings = True
        elif a in ("-R", "--just-symbols"):
            skip = True                     # its symbols only: GNU ld does not read its notes
        elif not a.startswith("-") and os.path.isfile(a):
            inputs.append((a, whole))           # --whole-archive applies to what follows it
    machine = None
    note = exec_obj = empty_obj = None
    for path, whole in inputs:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if data[:8] in (b"!<arch>\n", b"!<thin>\n"):
            objs = [(f"{path}({n})", d) for n, d in archive_members(path, data)] if whole else []
        else:
            objs = [(path, data)]
        for name, d in objs:
            r = elf_note(d)
            if r is None:
                continue
            m, has, ex = r
            machine = m if machine is None else machine
            if has:
                note = name
                if ex:
                    exec_obj = name
                    break
            elif m not in NO_DEFAULT_EXECSTACK and empty_obj is None:
                empty_obj = name
        if exec_obj:
            break
    if not (note or stack_size):
        return ""
    if exec_obj:
        if warnings:
            warn(f"warning: {exec_obj}: requires executable stack (because the .note.GNU-stack "
                 "section is executable)")
        return "-zexecstack"
    if empty_obj:
        if warnings:
            warn(f"warning: {empty_obj}: missing .note.GNU-stack section implies executable stack")
            warn("NOTE: This behaviour is deprecated and will be removed in a future version of the linker")
        return "-zexecstack"
    return "-znoexecstack"


def main(argv):
    out = decide(argv, warn=lambda m: print(f"ld.eld (eldconv): {m}", file=sys.stderr))
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
