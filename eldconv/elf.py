"""A small, read-only ELF reader.

`check` looks at every file a build linked, and some of them are large (a debug vmlinux is
hundreds of MB): the file is mapped, not read, and parsed once for all the questions asked
of it. Anything malformed -- truncated, absurd offsets, not an ELF at all -- gives None or
an empty answer, never an exception: a build tree holds fuzz inputs and half-written files.
"""
from __future__ import annotations

import mmap
import os
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

ET = {1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}

PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_NOTE, PT_TLS = 1, 2, 3, 4, 7
PT_GNU_STACK, PT_GNU_RELRO = 0x6474E551, 0x6474E552
PF_X, PF_W, PF_R = 1, 2, 4

SHT_SYMTAB, SHT_STRTAB, SHT_RELA, SHT_DYNAMIC, SHT_NOTE, SHT_NOBITS, SHT_REL = 2, 3, 4, 6, 7, 8, 9
SHT_DYNSYM, SHT_RELR = 11, 19
SHT_GNU_VERDEF, SHT_GNU_VERNEED, SHT_GNU_VERSYM = 0x6FFFFFFD, 0x6FFFFFFE, 0x6FFFFFFF
SHF_WRITE, SHF_ALLOC, SHF_EXECINSTR, SHF_TLS = 1, 2, 4, 0x400

DT_NEEDED, DT_STRTAB, DT_SONAME, DT_RPATH, DT_TEXTREL = 1, 5, 14, 15, 22
DT_FLAGS, DT_RUNPATH, DT_FLAGS_1 = 30, 29, 0x6FFFFFFB
DF_TEXTREL, DF_BIND_NOW = 4, 8
DF_1_NOW, DF_1_PIE = 1, 0x08000000

STB_GLOBAL, STB_WEAK = 1, 2
STT_OBJECT, STT_FUNC, STT_TLS = 1, 2, 6
SHN_UNDEF, SHN_ABS = 0, 0xFFF1

EM = {3: "x86", 8: "mips", 20: "ppc", 21: "ppc64", 40: "arm", 62: "x86_64", 164: "hexagon",
      183: "aarch64", 243: "riscv"}


class Phdr(NamedTuple):
    type: int
    flags: int
    offset: int
    vaddr: int
    paddr: int
    filesz: int
    memsz: int
    align: int


class Shdr(NamedTuple):
    name: str
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    entsize: int


class Sym(NamedTuple):
    name: str
    value: int
    size: int
    bind: int
    type: int
    visibility: int
    shndx: int
    index: int


class Malformed(ValueError):
    pass


@dataclass
class ELF:
    path: str
    data: "mmap.mmap | bytes"
    is64: bool
    le: bool
    type: int
    machine: int
    entry: int
    phdrs: List[Phdr]
    sections: List[Shdr]

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, path) -> Optional["ELF"]:
        """The parsed headers of `path`, or None if it is not a readable ELF file."""
        try:
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                if size < 52:
                    return None
                if fh.read(4) != b"\x7fELF":
                    return None
                fh.seek(0)
                try:
                    data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                except (OSError, ValueError):
                    data = fh.read()
        except OSError:
            return None
        try:
            return cls._parse(str(path), data)
        except (Malformed, struct.error, IndexError, OverflowError, ValueError, MemoryError):
            return None

    @classmethod
    def from_bytes(cls, data: bytes, path: str = "<bytes>") -> Optional["ELF"]:
        try:
            return cls._parse(path, data)
        except (Malformed, struct.error, IndexError, OverflowError, ValueError, MemoryError):
            return None

    @classmethod
    def _parse(cls, path: str, data) -> "ELF":
        if len(data) < 52 or data[:4] != b"\x7fELF" or data[4] not in (1, 2) or data[5] not in (1, 2):
            raise Malformed("not an ELF file")
        is64, le = data[4] == 2, data[5] == 1
        e = "<" if le else ">"
        if is64:
            if len(data) < 64:
                raise Malformed("truncated header")
            (etype, machine, _, entry, phoff, shoff, _, _, phentsize, phnum, shentsize, shnum,
             shstrndx) = struct.unpack_from(e + "HHIQQQIHHHHHH", data, 16)
        else:
            (etype, machine, _, entry, phoff, shoff, _, _, phentsize, phnum, shentsize, shnum,
             shstrndx) = struct.unpack_from(e + "HHIIIIIHHHHHH", data, 16)
        phdrs: List[Phdr] = []
        if phoff and phnum:
            if phentsize < (56 if is64 else 32) or phoff + phnum * phentsize > len(data):
                raise Malformed("program headers outside the file")
            for i in range(phnum):
                off = phoff + i * phentsize
                if is64:
                    t, fl, o, va, pa, fs, ms, al = struct.unpack_from(e + "IIQQQQQQ", data, off)
                else:
                    t, o, va, pa, fs, ms, fl, al = struct.unpack_from(e + "IIIIIIII", data, off)
                phdrs.append(Phdr(t, fl, o, va, pa, fs, ms, al))
        raw = []
        if shoff and shnum:
            if shentsize < (64 if is64 else 40) or shoff + shnum * shentsize > len(data):
                raise Malformed("section headers outside the file")
            for i in range(shnum):
                off = shoff + i * shentsize
                if is64:
                    raw.append(struct.unpack_from(e + "IIQQQQIIQQ", data, off))
                else:
                    raw.append(struct.unpack_from(e + "IIIIIIIIII", data, off))
        sections: List[Shdr] = []
        stroff = raw[shstrndx][4] if shstrndx < len(raw) else None
        for name, t, fl, addr, off, size, link, info, _, entsize in raw:
            sections.append(Shdr(_cstr(data, stroff + name) if stroff is not None else "",
                                 t, fl, addr, off, size, link, info, entsize))
        return cls(path, data, is64, le, etype, machine, entry, phdrs, sections)

    # ------------------------------------------------------------------ basics
    @property
    def endian(self) -> str:
        return "<" if self.le else ">"

    @property
    def type_name(self) -> str:
        return ET.get(self.type, str(self.type))

    @property
    def machine_name(self) -> str:
        return EM.get(self.machine, str(self.machine))

    def section(self, name: str) -> Optional[Shdr]:
        return next((s for s in self.sections if s.name == name), None)

    def of_type(self, t: int) -> List[Shdr]:
        return [s for s in self.sections if s.type == t]

    def bytes_of(self, s: Shdr) -> bytes:
        if s.type == SHT_NOBITS or s.offset + s.size > len(self.data):
            return b""
        return bytes(self.data[s.offset:s.offset + s.size])

    def cstr(self, offset: int) -> str:
        return _cstr(self.data, offset)

    # ------------------------------------------------------------------ symbols
    def symbols(self, s: Optional[Shdr]) -> Iterator[Sym]:
        """The entries of a SHT_SYMTAB/SHT_DYNSYM section (index 0, the null symbol, skipped)."""
        if s is None or s.link >= len(self.sections):
            return
        esz = s.entsize or (24 if self.is64 else 16)
        stroff = self.sections[s.link].offset
        e = self.endian
        end = min(s.offset + s.size, len(self.data))
        for idx, pos in enumerate(range(s.offset + esz, end - esz + 1, esz), 1):
            if self.is64:
                name, info, other, shndx, value, size = struct.unpack_from(e + "IBBHQQ", self.data, pos)
            else:
                name, value, size, info, other, shndx = struct.unpack_from(e + "IIIBBH", self.data, pos)
            yield Sym(self.cstr(stroff + name) if name else "", value, size, info >> 4, info & 0xF,
                      other & 3, shndx, idx)

    @cached_property
    def dynsym(self) -> List[Sym]:
        return list(self.symbols(next(iter(self.of_type(SHT_DYNSYM)), None)))

    @cached_property
    def verdef_names(self) -> Set[str]:
        """Versions this file defines; each also appears as an absolute symbol of that name."""
        names: Set[str] = set()
        e = self.endian
        for s in self.of_type(SHT_GNU_VERDEF):
            stroff = self.sections[s.link].offset
            pos = s.offset
            for _ in range(s.info):
                _, _, _, cnt, _, aux, nxt = struct.unpack_from(e + "HHHHIII", self.data, pos)
                if cnt:
                    names.add(self.cstr(stroff + struct.unpack_from(e + "I", self.data, pos + aux)[0]))
                if not nxt:
                    break
                pos += nxt
        return names

    @cached_property
    def versions(self) -> Dict[int, str]:
        """dynsym index -> version name, for symbols that carry one."""
        versym = next(iter(self.of_type(SHT_GNU_VERSYM)), None)
        if versym is None:
            return {}
        names: Dict[int, str] = {}
        e = self.endian
        for s in self.of_type(SHT_GNU_VERDEF):
            stroff = self.sections[s.link].offset
            pos = s.offset
            for _ in range(s.info):
                _, _, ndx, cnt, _, aux, nxt = struct.unpack_from(e + "HHHHIII", self.data, pos)
                if cnt:
                    names[ndx] = self.cstr(stroff + struct.unpack_from(e + "I", self.data, pos + aux)[0])
                if not nxt:
                    break
                pos += nxt
        for s in self.of_type(SHT_GNU_VERNEED):
            stroff = self.sections[s.link].offset
            pos = s.offset
            for _ in range(s.info):
                _, cnt, _, aux, nxt = struct.unpack_from(e + "HHIII", self.data, pos)
                apos = pos + aux
                for _ in range(cnt):
                    _, _, other, name, anext = struct.unpack_from(e + "IHHII", self.data, apos)
                    names[other] = self.cstr(stroff + name)
                    if not anext:
                        break
                    apos += anext
                if not nxt:
                    break
                pos += nxt
        out: Dict[int, str] = {}
        for i in range(1, versym.size // 2):
            v = struct.unpack_from(e + "H", self.data, versym.offset + 2 * i)[0] & 0x7FFF
            if v > 1 and v in names:
                out[i] = names[v]
        return out

    # ------------------------------------------------------------------ dynamic
    @cached_property
    def dynamic(self) -> List[Tuple[int, int]]:
        s = next(iter(self.of_type(SHT_DYNAMIC)), None)
        if s is None:
            p = next((p for p in self.phdrs if p.type == PT_DYNAMIC), None)
            if p is None:
                return []
            off, size = p.offset, p.filesz
        else:
            off, size = s.offset, s.size
        fmt, esz = (self.endian + "qQ", 16) if self.is64 else (self.endian + "iI", 8)
        out = []
        for pos in range(off, min(off + size, len(self.data)) - esz + 1, esz):
            tag, val = struct.unpack_from(fmt, self.data, pos)
            if tag == 0:
                break
            out.append((tag, val))
        return out

    def _dynstr_offset(self) -> Optional[int]:
        s = self.section(".dynstr")
        if s is not None:
            return s.offset
        addr = next((v for t, v in self.dynamic if t == DT_STRTAB), None)
        return None if addr is None else self.offset_of(addr)

    def _dyn_strings(self, tag: int) -> List[str]:
        base = self._dynstr_offset()
        if base is None:
            return []
        return [self.cstr(base + v) for t, v in self.dynamic if t == tag]

    @cached_property
    def needed(self) -> List[str]:
        return self._dyn_strings(DT_NEEDED)

    @cached_property
    def soname(self) -> Optional[str]:
        return next(iter(self._dyn_strings(DT_SONAME)), None)

    @cached_property
    def runpath(self) -> Optional[str]:
        return next(iter(self._dyn_strings(DT_RUNPATH) or self._dyn_strings(DT_RPATH)), None)

    def dyn_value(self, tag: int) -> Optional[int]:
        return next((v for t, v in self.dynamic if t == tag), None)

    @property
    def bind_now(self) -> bool:
        return bool((self.dyn_value(DT_FLAGS) or 0) & DF_BIND_NOW or (self.dyn_value(DT_FLAGS_1) or 0) & DF_1_NOW)

    @property
    def textrel(self) -> bool:
        return self.dyn_value(DT_TEXTREL) is not None or bool((self.dyn_value(DT_FLAGS) or 0) & DF_TEXTREL)

    @property
    def is_pie(self) -> bool:
        return self.type == 3 and (bool((self.dyn_value(DT_FLAGS_1) or 0) & DF_1_PIE)
                                   or self.interp is not None)

    @cached_property
    def interp(self) -> Optional[str]:
        p = next((p for p in self.phdrs if p.type == PT_INTERP), None)
        if p is None or p.offset + p.filesz > len(self.data):
            return None
        return bytes(self.data[p.offset:p.offset + p.filesz]).split(b"\0", 1)[0].decode(errors="replace")

    def offset_of(self, vaddr: int) -> Optional[int]:
        for p in self.phdrs:
            if p.type == PT_LOAD and p.vaddr <= vaddr < p.vaddr + p.filesz:
                return p.offset + vaddr - p.vaddr
        return None

    # ------------------------------------------------------------------ notes, comment
    @cached_property
    def build_id(self) -> Optional[str]:
        e = self.endian
        for s in self.of_type(SHT_NOTE):
            pos, end = s.offset, min(s.offset + s.size, len(self.data))
            while pos + 12 <= end:
                namesz, descsz, ntype = struct.unpack_from(e + "III", self.data, pos)
                nstart = pos + 12
                dstart = nstart + ((namesz + 3) & ~3)
                if ntype == 3 and bytes(self.data[nstart:nstart + namesz]).rstrip(b"\0") == b"GNU":
                    return bytes(self.data[dstart:dstart + descsz]).hex()
                pos = dstart + ((descsz + 3) & ~3)
        return None

    @cached_property
    def comment(self) -> str:
        s = self.section(".comment")
        return self.bytes_of(s).replace(b"\0", b" ").decode(errors="replace") if s else ""

    # ------------------------------------------------------------------ segments
    def load_size(self) -> int:
        return sum(p.memsz for p in self.phdrs if p.type == PT_LOAD)

    @property
    def exec_stack(self) -> Optional[bool]:
        """True/False from PT_GNU_STACK; None when there is none (the loader's default)."""
        p = next((p for p in self.phdrs if p.type == PT_GNU_STACK), None)
        return None if p is None else bool(p.flags & PF_X)

    @property
    def has_relro(self) -> bool:
        return any(p.type == PT_GNU_RELRO for p in self.phdrs)

    def close(self) -> None:
        if isinstance(self.data, mmap.mmap):
            self.data.close()

    def __enter__(self) -> "ELF":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _cstr(data, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return bytes(data[offset:end]).decode(errors="replace")


@contextmanager
def open_elf(path) -> Iterator[Optional[ELF]]:
    """`with open_elf(p) as elf:` -- None if `p` is not an ELF file; unmapped afterwards."""
    elf = ELF.open(Path(path))
    try:
        yield elf
    finally:
        if elf is not None:
            elf.close()
