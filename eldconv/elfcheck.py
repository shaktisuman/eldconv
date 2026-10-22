"""Judge the ELF files eld produced: are they well formed, and do they differ from what the
original linker made of the same inputs?

`validate` reads one output on its own: headers and tables that point outside the file,
segments that overlap or are misaligned, an entry point no executable segment holds,
allocated sections no segment loads. Any of these means a broken image, whatever linked it.

`compare` reads an output next to the reference build's file of the same name and reports
what a user of the binary would notice: a different interpreter or SONAME, libraries no
longer needed (or newly needed), symbols no longer exported or with another version, lost
hardening (RELRO, BIND_NOW, a non-executable stack, PIE), missing sections and symbols, and
size; then, through layout.compare_layout, how the image is laid out: segments, permissions,
order, placement and the symbols that mark section edges.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .elf import (DT_NEEDED, DT_RPATH, DT_RUNPATH, DT_SONAME, ELF, PF_W, PF_X, PT_DYNAMIC, PT_INTERP,
                  PT_LOAD, SHF_ALLOC, SHF_TLS, SHN_ABS, SHN_UNDEF, SHT_DYNSYM, SHT_NOBITS, SHT_NOTE, SHT_SYMTAB,
                  STB_GLOBAL, STB_WEAK)


@dataclass
class Finding:
    level: str          # "error" | "warn" | "info"
    what: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "what": self.what, "message": self.message}


# ---------------------------------------------------------------------------- validate

# What a missing PT_GNU_STACK does: glibc's default stack permissions (DEFAULT_STACK_PROT_PERMS)
# include execute on these machines (EM_*): SPARC, i386, m68k, MIPS, PA-RISC, PowerPC (32-bit),
# s390, ARM, SuperH, x86_64, OpenRISC, MicroBlaze, Alpha. AArch64, RISC-V, LoongArch and the
# others use the generic, non-executable default.
EXEC_STACK_DEFAULT = frozenset({2, 3, 4, 8, 10, 15, 18, 20, 22, 40, 42, 43, 62, 92, 189, 0x9026})
_NO_STACK = ("thread stacks are mapped executable, and glibc 2.41 and later refuse to dlopen "
             "such a library")

_DT_STRINGS = (DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH)


def validate(elf: ELF) -> List[Finding]:
    out: List[Finding] = []
    size = len(elf.data)

    def bad(what: str, msg: str, level: str = "error") -> None:
        out.append(Finding(level, what, msg))

    for s in elf.sections:
        if s.type != SHT_NOBITS and s.size and s.offset + s.size > size:
            bad("section", f"{s.name or '?'} ends at file offset {s.offset + s.size:#x}, past the "
                           f"end of the file ({size:#x})")
    loads = [p for p in elf.phdrs if p.type == PT_LOAD]
    for p in loads:
        where = f"PT_LOAD at {p.vaddr:#x}"
        if p.filesz > p.memsz:
            bad("segment", f"{where}: file size {p.filesz:#x} is larger than its memory size {p.memsz:#x}")
        if p.offset + p.filesz > size:
            bad("segment", f"{where}: its contents end past the end of the file")
        if p.align > 1:
            if p.align & (p.align - 1):
                bad("segment", f"{where}: alignment {p.align:#x} is not a power of two")
            elif (p.vaddr - p.offset) % p.align:
                bad("segment", f"{where}: address and file offset {p.offset:#x} disagree modulo the "
                               f"alignment {p.align:#x}, so it cannot be mapped")
    spans = sorted((p.vaddr, p.vaddr + p.memsz) for p in loads if p.memsz)
    for (lo1, hi1), (lo2, hi2) in zip(spans, spans[1:]):
        if lo2 < hi1:
            bad("segment", f"PT_LOAD segments overlap: [{lo1:#x}, {hi1:#x}) and [{lo2:#x}, {hi2:#x})")
    for p in elf.phdrs:
        if p.type in (PT_INTERP, PT_DYNAMIC) and p.offset + p.filesz > size:
            bad("segment", f"{'PT_INTERP' if p.type == PT_INTERP else 'PT_DYNAMIC'} lies past the end of the file")
    # objcopy --only-keep-debug: the headers of an image and its notes, none of its contents
    debug_only = all(s.type in (SHT_NOBITS, SHT_NOTE) for s in elf.sections if s.flags & SHF_ALLOC and s.size)
    if elf.type in (2, 3) and loads and not debug_only:
        if elf.entry and not any(
                p.flags & PF_X and (p.vaddr <= elf.entry < p.vaddr + p.memsz
                                    or p.paddr <= elf.entry < p.paddr + p.memsz) for p in loads):
            bad("entry", f"entry point {elf.entry:#x} is in no executable segment")
        for s in elf.sections:
            if not (s.flags & SHF_ALLOC) or not s.size or (s.flags & SHF_TLS and s.type == SHT_NOBITS):
                continue
            if not any(p.vaddr <= s.addr and s.addr + s.size <= p.vaddr + p.memsz for p in loads):
                bad("section", f"allocated section {s.name or '?'} at [{s.addr:#x}, {s.addr + s.size:#x}) "
                               "is outside every loadable segment, so it is not loaded")
        interp = next((p for p in elf.phdrs if p.type == PT_INTERP), None)
        if interp is not None and (not elf.interp or b"\0" not in bytes(
                elf.data[interp.offset:interp.offset + interp.filesz])):
            bad("interp", "PT_INTERP does not hold a NUL-terminated path")
        if any(t in _DT_STRINGS for t, _ in elf.dynamic) and elf._dynstr_offset() is None:
            bad("dynamic", "the dynamic section names libraries or paths but has no string table")
        for i, n in enumerate(elf.needed):
            if not n:
                bad("dynamic", f"DT_NEEDED entry {i} names no library")
        for p in loads:
            if p.flags & PF_W and p.flags & PF_X:
                bad("segment", f"PT_LOAD at {p.vaddr:#x} is writable and executable", "warn")
        if elf.exec_stack:
            bad("stack", "PT_GNU_STACK asks for an executable stack", "warn")
        elif (elf.exec_stack is None and elf.machine in EXEC_STACK_DEFAULT
              and (elf.interp or elf.needed or elf.soname)):
            # (what ld.so loads; a kernel image has a dynamic section, but none of these)
            # eld writes PT_GNU_STACK only for an explicit -z [no]execstack: it does not read
            # the inputs' .note.GNU-stack sections as GNU ld does
            bad("stack", f"has no PT_GNU_STACK, so the dynamic loader assumes it needs an executable "
                         f"stack ({_NO_STACK}); link with -z noexecstack", "warn")
        if elf.type == 3 and elf.textrel:
            bad("dynamic", "has text relocations (DT_TEXTREL): its code pages are written at load time",
                "warn")
    nsec = len(elf.sections)
    for t in (SHT_SYMTAB, SHT_DYNSYM):
        tab = next(iter(elf.of_type(t)), None)
        if tab is None:
            continue
        if tab.link >= nsec:
            bad("symbols", f"{tab.name} names string table {tab.link}, which does not exist")
            continue
        wild = [s.name for s in elf.symbols(tab)
                if s.shndx != SHN_UNDEF and s.shndx < 0xFF00 and s.shndx >= nsec]
        if wild:
            bad("symbols", f"{len(wild)} symbol(s) in {tab.name} point at sections that do not exist: "
                           + ", ".join(wild[:5]))
        if t == SHT_DYNSYM and elf.type in (2, 3) and not debug_only:
            stray = []
            for s in elf.symbols(tab):
                if (s.shndx in (SHN_UNDEF, SHN_ABS) or s.shndx >= nsec or s.type == 6   # TLS: offsets
                        or s.name in LINKER_SYMBOLS):   # _end and friends mark where a section ends
                    continue
                sec = elf.sections[s.shndx]
                if sec.flags & SHF_ALLOC and not (sec.addr <= s.value <= sec.addr + sec.size):
                    stray.append(s.name)
            if stray:
                bad("symbols", f"{len(stray)} exported symbol(s) lie outside the section they belong to: "
                               + ", ".join(stray[:5]))
    return out


# ---------------------------------------------------------------------------- compare

# Sections a linker makes up for itself; which of them exist depends on the linker and its
# defaults (hash style, RELR, PLT layout), so their presence is reported as information only.
LINKER_SECTIONS = frozenset({
    ".interp", ".dynamic", ".dynsym", ".dynstr", ".hash", ".gnu.hash", ".gnu.version",
    ".gnu.version_r", ".gnu.version_d", ".rela.dyn", ".rel.dyn", ".relr.dyn", ".rela.plt",
    ".rel.plt", ".plt", ".plt.got", ".plt.sec", ".iplt", ".got", ".got.plt", ".igot", ".igot.plt",
    ".eh_frame_hdr", ".note.gnu.property", ".note.gnu.build-id", ".note.ABI-tag", ".comment",
    ".tm_clone_table", ".branch_lt", ".ARM.exidx", ".ARM.attributes", ".riscv.attributes",
    ".symtab", ".strtab", ".shstrtab",
})

# Symbols linkers define themselves, each its own set of them.
LINKER_SYMBOLS = frozenset({
    "__bss_start", "_edata", "edata", "_end", "end", "__end", "_etext", "etext", "__etext",
    "__ehdr_start", "__executable_start", "_GLOBAL_OFFSET_TABLE_", "_DYNAMIC",
    "__GNU_EH_FRAME_HDR", "_PROCEDURE_LINKAGE_TABLE_", "__TMC_END__", "__dso_handle",
    "__init_array_start", "__init_array_end", "__fini_array_start", "__fini_array_end",
    "__preinit_array_start", "__preinit_array_end", "__rela_iplt_start", "__rela_iplt_end",
    "__rel_iplt_start", "__rel_iplt_end", "__data_start", "data_start", "__global_pointer$",
    "__bss_start__", "__bss_end__", "_bss_end__", "__end__", "__exidx_start", "__exidx_end",
    "_fbss", "_fdata", "_ftext", "__stack", "_stack",
})

SIZE_TOLERANCE = 0.10
SIZE_SLACK = 64 * 1024


def _version_symbol(elf: ELF, s) -> bool:
    """The absolute symbol a version definition (FOO_1 { ... }) adds under its own name."""
    return s.shndx == SHN_ABS and s.name in elf.verdef_names


def _copy_relocated(elf: ELF) -> Set[str]:
    """Data an executable defines only as the target of a copy relocation (`stdout` copied
    in from libc, versioned with a version libc defines), not as an export of its own."""
    vers, own = elf.versions, elf.verdef_names
    return {s.name for s in elf.dynsym
            if s.name and s.shndx not in (SHN_UNDEF, SHN_ABS) and s.index in vers
            and vers[s.index] not in own}


def _exports(elf: ELF) -> Dict[str, Optional[str]]:
    vers, copied = elf.versions, _copy_relocated(elf)
    return {s.name: vers.get(s.index) for s in elf.dynsym
            if s.name and s.shndx != SHN_UNDEF and s.bind in (STB_GLOBAL, STB_WEAK)
            and s.visibility in (0, 3) and not _version_symbol(elf, s) and s.name not in copied}


def _imports(elf: ELF) -> Set[str]:
    return {s.name for s in elf.dynsym if s.name and s.shndx == SHN_UNDEF and s.bind == STB_GLOBAL}


def _globals(elf: ELF) -> Set[str]:
    tab = next(iter(elf.of_type(SHT_SYMTAB)), None)
    # `stdout@GLIBC_2.2.5`: how a symbol table names a copy-relocated symbol
    return {s.name for s in elf.symbols(tab)
            if s.name and s.shndx != SHN_UNDEF and s.bind in (STB_GLOBAL, STB_WEAK)
            and not _version_symbol(elf, s) and "@" not in s.name}


def _sym_at(elf: ELF, addr: int) -> Optional[str]:
    tab = next(iter(elf.of_type(SHT_SYMTAB)), None) or next(iter(elf.of_type(SHT_DYNSYM)), None)
    names = sorted(s.name for s in elf.symbols(tab)
                   if s.name and s.value == addr and s.shndx != SHN_UNDEF and s.type in (0, 2))
    return names[0] if names else None


def _list(names, n: int = 8) -> str:
    names = sorted(names)
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def _related(name: str, others) -> bool:
    """`.data.rel.ro` and `.data.rel.ro.local`: one linker merges what the other keeps apart."""
    return any(o.startswith(name + ".") or name.startswith(o + ".") for o in others)


def compare(ref: ELF, new: ELF, roots: Optional[Tuple[str, str]] = None,
            scripted: bool = False) -> List[Finding]:
    """What changed from the reference linker's output (`ref`) to eld's (`new`).

    `roots` is (reference tree, converted tree): paths baked into the binaries (RUNPATH)
    differ by exactly that, which is not a difference. `scripted`: a linker script placed
    the image, so its layout is held to the reference's (layout.compare_layout).
    """
    from .layout import compare_layout
    out: List[Finding] = []

    def add(level: str, what: str, msg: str) -> None:
        out.append(Finding(level, what, msg))

    if (ref.is64, ref.le, ref.machine, ref.type) != (new.is64, new.le, new.machine, new.type):
        def ident(e: ELF) -> str:
            return f"{'ELF64' if e.is64 else 'ELF32'} {'LE' if e.le else 'BE'} {e.machine_name} {e.type_name}"
        add("error", "kind", f"is {ident(new)}; the reference is {ident(ref)}")
        return out
    if ref.interp != new.interp:
        add("error", "interpreter", f"PT_INTERP is {new.interp!r}; the reference has {ref.interp!r}")
    if ref.soname != new.soname:
        add("error", "soname", f"DT_SONAME is {new.soname!r}; the reference has {ref.soname!r}")
    gone = [n for n in ref.needed if n not in new.needed]
    extra = [n for n in new.needed if n not in ref.needed]
    if gone:
        add("error", "needed", f"no longer needs {_list(gone)} (DT_NEEDED)")
    if extra:
        add("warn", "needed", f"now also needs {_list(extra)} (DT_NEEDED)")
    new_runpath = new.runpath
    if roots and new_runpath:
        new_runpath = new_runpath.replace(roots[1], roots[0])
    if ref.runpath != new_runpath:
        add("warn", "runpath", f"RUNPATH/RPATH is {new.runpath!r}; the reference has {ref.runpath!r}")

    rx, nx = _exports(ref), _exports(new)
    lost = set(rx) - set(nx)
    if lost - LINKER_SYMBOLS:
        add("error", "exports", f"{len(lost - LINKER_SYMBOLS)} symbol(s) no longer exported: "
                                f"{_list(lost - LINKER_SYMBOLS)}")
    if lost & LINKER_SYMBOLS:
        add("info", "exports", f"linker-defined symbol(s) no longer exported: {_list(lost & LINKER_SYMBOLS)}")
    gained = set(nx) - set(rx) - LINKER_SYMBOLS
    if gained:
        why = (" (a version script's `local:` not applied?)" if ref.soname or ref.interp is None
               else "")
        add("warn", "exports", f"{len(gained)} symbol(s) exported that the reference keeps local{why}: "
                               f"{_list(gained)}")
    moved = {n for n in set(rx) & set(nx) if rx[n] != nx[n]}
    if moved:
        sample = ", ".join(f"{n}@{nx[n] or '-'} (was @{rx[n] or '-'})" for n in sorted(moved)[:5])
        add("error", "versions", f"{len(moved)} exported symbol(s) changed version: {sample}")
    needs = _imports(new) - _imports(ref) - _copy_relocated(ref)   # those are unresolved data
    if needs:
        add("warn", "imports", f"{len(needs)} undefined symbol(s) the reference does not need: {_list(needs)}")

    if ref.exec_stack is False and new.exec_stack is None and new.machine in EXEC_STACK_DEFAULT:
        add("error", "stack", f"has no PT_GNU_STACK, so it needs an executable stack ({_NO_STACK}); "
                              "the reference marks the stack non-executable")
    if not ref.exec_stack and new.exec_stack:
        add("error", "stack", "asks for an executable stack; the reference does not")
    if ref.has_relro and not new.has_relro:
        add("error", "relro", "has no PT_GNU_RELRO: relocated data stays writable (the reference is RELRO)")
    if ref.bind_now and not new.bind_now:
        add("error", "bind-now", "binds lazily; the reference resolves every symbol at load (BIND_NOW)")
    if ref.is_pie and not new.is_pie:
        add("error", "pie", "is not position independent; the reference is a PIE")
    if new.textrel and not ref.textrel:
        add("warn", "textrel", "has text relocations; the reference has none")
    if ref.build_id and not new.build_id:
        add("warn", "build-id", "has no GNU build-id note; the reference has one")

    if ref.entry and new.entry:
        a, b = _sym_at(ref, ref.entry), _sym_at(new, new.entry)
        if a and b and a != b:
            add("warn", "entry", f"entry point is {b}; the reference starts at {a}")
    elif bool(ref.entry) != bool(new.entry):
        add("warn", "entry", f"entry point is {new.entry:#x}; the reference has {ref.entry:#x}")

    def alloc(e: ELF) -> Dict[str, int]:
        d: Dict[str, int] = {}
        for s in e.sections:
            if s.flags & SHF_ALLOC and s.size:
                d[s.name] = d.get(s.name, 0) + s.size
        return d
    rs, ns = alloc(ref), alloc(new)
    missing = {x for x in set(rs) - set(ns) if not _related(x, ns)}
    if missing - LINKER_SECTIONS:
        add("warn", "sections", f"allocated section(s) the reference has are missing: "
                                f"{_list(missing - LINKER_SECTIONS)}")
    if missing & LINKER_SECTIONS:
        add("info", "sections", f"linker-generated section(s) not present: {_list(missing & LINKER_SECTIONS)}")
    if set(ns) - set(rs):
        add("info", "sections", f"section(s) the reference does not have: {_list(set(ns) - set(rs))}")

    rg, ng = _globals(ref), _globals(new)
    lost_g = rg - ng
    if lost_g - LINKER_SYMBOLS:
        add("warn", "symbols", f"{len(lost_g - LINKER_SYMBOLS)} global symbol(s) the reference defines are "
                               f"missing: {_list(lost_g - LINKER_SYMBOLS)}")
    if lost_g & LINKER_SYMBOLS:
        add("info", "symbols", f"linker-defined symbol(s) not present: {_list(lost_g & LINKER_SYMBOLS)}")

    r, n = ref.load_size(), new.load_size()
    if r and n:
        delta = (n - r) / r
        # a page more or less is a large share of a small program, and means nothing
        level = "warn" if abs(delta) > SIZE_TOLERANCE and abs(n - r) > SIZE_SLACK else "info"
        add(level, "size", f"loads {n} bytes, {delta:+.1%} against the reference's {r}")
    tr, tn = rs.get(".text"), ns.get(".text")
    if tr and tn and tr != tn:
        add("info", "size", f".text is {tn} bytes, {(tn - tr) / tr:+.1%} against the reference's {tr}")
    out.extend(compare_layout(ref, new, scripted))
    return out
