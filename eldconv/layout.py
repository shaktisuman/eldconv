"""Where things are in an image, and how that differs between two links of the same inputs.

`layout` describes one file: its segments (type, permissions, addresses, alignment, the
sections each one loads) and its allocated sections. `compare_layout` reads eld's output next
to the reference linker's and reports the differences that change how the image loads or runs:

- a segment kind the reference has and eld's output lacks (PT_TLS, PT_GNU_EH_FRAME, ...);
- a section mapped with other permissions (code without execute, data written at run time
  but loaded read-only), or no longer covered by PT_GNU_RELRO;
- PT_LOAD alignment smaller than the reference's (the image no longer maps on larger pages);
- for an image placed by a linker script (`-T`): another base address, sections in another
  order, or a section loaded at another physical address (LMA);
- a symbol that marks the start or end of a section (`__bss_start`, `__start_foo`, `_etext`)
  no longer doing so, and a `*_start`/`*_end` range that holds data in the reference and is
  empty in eld's output.

Addresses that merely shift because an earlier section grew or shrank are expected between two
linkers and only recorded as information.
"""
from __future__ import annotations

import bisect
from typing import Dict, List, Set, Tuple

from .elf import (ELF, PF_R, PF_W, PF_X, PT_DYNAMIC, PT_GNU_RELRO, PT_GNU_STACK, PT_INTERP, PT_LOAD,
                  PT_NOTE, PT_TLS, SHF_ALLOC, SHF_EXECINSTR, SHF_TLS, SHF_WRITE, SHN_ABS, SHN_UNDEF,
                  SHT_DYNSYM, SHT_NOBITS, SHT_SYMTAB, STB_GLOBAL, STB_WEAK, Shdr)
from .elfcheck import LINKER_SYMBOLS, Finding

PT_PHDR, PT_GNU_EH_FRAME, PT_GNU_PROPERTY = 6, 0x6474E550, 0x6474E553
PT_ARM_EXIDX = 0x70000001
STT_NOTYPE = 0

PT_NAMES = {PT_LOAD: "LOAD", PT_DYNAMIC: "DYNAMIC", PT_INTERP: "INTERP", PT_NOTE: "NOTE",
            PT_PHDR: "PHDR", PT_TLS: "TLS", PT_GNU_EH_FRAME: "GNU_EH_FRAME", PT_GNU_STACK: "GNU_STACK",
            PT_GNU_RELRO: "GNU_RELRO", PT_GNU_PROPERTY: "GNU_PROPERTY", PT_ARM_EXIDX: "ARM_EXIDX",
            0x70000003: "RISCV_ATTRIBUTES"}

# Segment kinds whose loss changes what the loader or the runtime can do. PT_INTERP,
# PT_DYNAMIC, PT_GNU_RELRO and PT_GNU_STACK are judged by elfcheck.compare already.
_SEGMENT_LOSS = {
    PT_TLS: ("error", "thread-local variables have no initial image"),
    PT_ARM_EXIDX: ("error", "the ARM unwinder cannot find its tables"),
    PT_GNU_EH_FRAME: ("warn", "the unwinder cannot find .eh_frame_hdr (C++ exceptions and "
                              "backtraces may fail)"),
    PT_GNU_PROPERTY: ("warn", "CET/BTI markings in .note.gnu.property are not applied"),
    PT_NOTE: ("info", "notes are not loaded"),
}


def perms(flags: int) -> str:
    return (("R" if flags & PF_R else "") + ("W" if flags & PF_W else "")
            + ("X" if flags & PF_X else "")) or "-"


def _sec_flags(s: Shdr) -> str:
    return ("W" if s.flags & SHF_WRITE else "") + ("A" if s.flags & SHF_ALLOC else "") \
        + ("X" if s.flags & SHF_EXECINSTR else "") + ("T" if s.flags & SHF_TLS else "")


def _placed(e: ELF) -> List[Shdr]:
    """Allocated, non-empty sections that occupy addresses (.tbss overlaps what follows it)."""
    return [s for s in e.sections
            if s.flags & SHF_ALLOC and s.size and s.name
            and not (s.flags & SHF_TLS and s.type == SHT_NOBITS)]


def _load_of(e: ELF, s: Shdr):
    for p in e.phdrs:
        if p.type == PT_LOAD and p.vaddr <= s.addr < p.vaddr + max(p.memsz, 1):
            return p
    return None


def _inside(e: ELF, s: Shdr, ptype: int) -> bool:
    return any(p.type == ptype and p.vaddr <= s.addr and s.addr + s.size <= p.vaddr + p.memsz
               for p in e.phdrs)


def layout(e: ELF) -> dict:
    """The segments and allocated sections of `e`, for reports."""
    secs = _placed(e)
    segs = []
    for p in e.phdrs:
        inside = [s.name for s in secs if p.memsz and p.vaddr <= s.addr < p.vaddr + p.memsz]
        segs.append({"type": PT_NAMES.get(p.type, f"{p.type:#x}"), "flags": perms(p.flags),
                     "offset": p.offset, "vaddr": p.vaddr, "paddr": p.paddr, "filesz": p.filesz,
                     "memsz": p.memsz, "align": p.align, "sections": inside})
    sections = []
    for s in secs:
        p = _load_of(e, s)
        sections.append({"name": s.name, "addr": s.addr, "size": s.size, "flags": _sec_flags(s),
                         "lma": s.addr + (p.paddr - p.vaddr) if p else None,
                         "segment": perms(p.flags) if p else None})
    return {"segments": segs, "sections": sections}


def _names(xs, n: int = 6) -> str:
    xs = list(xs)
    return ", ".join(xs[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


def _moved(ref_order: List[str], new_order: List[str]) -> List[str]:
    """Sections not on a longest common ordering: the ones placed somewhere else."""
    pos = {n: i for i, n in enumerate(new_order)}
    seq = [pos[n] for n in ref_order if n in pos]
    names = [n for n in ref_order if n in pos]
    # patience-style LIS, O(n log n), with back links to recover the kept elements
    tails: List[int] = []
    tail_idx: List[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    keep: Set[int] = set()
    i = tail_idx[-1] if tail_idx else -1
    while i >= 0:
        keep.add(i)
        i = prev[i]
    return [names[i] for i in range(len(seq)) if i not in keep]


# ------------------------------------------------------------------ boundary symbols

def _markers(e: ELF) -> Dict[str, int]:
    """Symbols that mark a place rather than name an object: NOTYPE, size 0. A linker script's
    `__bss_start = .;` and the `__start_SEC`/`__stop_SEC` a linker defines are such; local
    ones only with a `__` name (assembler labels are local too, and say nothing about layout)."""
    tab = next(iter(e.of_type(SHT_SYMTAB)), None) or next(iter(e.of_type(SHT_DYNSYM)), None)
    out: Dict[str, int] = {}
    if tab is None:
        return out
    nsec = len(e.sections)
    for s in e.symbols(tab):
        if (s.type != STT_NOTYPE or s.size or not s.name or s.shndx in (SHN_UNDEF, SHN_ABS)
                or s.shndx >= nsec or s.name.startswith(("$", ".L"))):
            continue
        if s.bind in (STB_GLOBAL, STB_WEAK) or s.name.startswith("__"):
            out.setdefault(s.name, s.value)
    return out


def _edges(secs: List[Shdr], value: int) -> Set[Tuple[str, str]]:
    found = set()
    for s in secs:
        if s.addr == value:
            found.add((s.name, "start"))
        if s.addr + s.size == value:
            found.add((s.name, "end"))
    return found


def _pairs(names) -> List[Tuple[str, str]]:
    """(start, end) marker pairs: __start_X/__stop_X, X_start/X_end, X_begin/X_end."""
    out = []
    for n in names:
        if n.startswith("__start_"):
            out.append((n, "__stop_" + n[len("__start_"):]))
        for a, b in (("_start", "_end"), ("_begin", "_end")):
            if n.endswith(a) and len(n) > len(a):
                out.append((n, n[: -len(a)] + b))
    return [(a, b) for a, b in out if b in names]


# ------------------------------------------------------------------------- compare

def compare_layout(ref: ELF, new: ELF, scripted: bool = False) -> List[Finding]:
    """How eld's layout of an image (`new`) differs from the reference linker's (`ref`).

    `scripted`: the link placed the image with a linker script (`-T`), so its addresses are a
    contract (a kernel, firmware, a boot loader) rather than the linker's choice.
    """
    out: List[Finding] = []

    def add(level: str, what: str, msg: str) -> None:
        out.append(Finding(level, what, msg))

    # segment kinds
    have = {p.type for p in new.phdrs}
    for t in sorted({p.type for p in ref.phdrs} - have):
        if t in _SEGMENT_LOSS:
            level, why = _SEGMENT_LOSS[t]
            add(level, "segments", f"has no PT_{PT_NAMES[t]} segment; the reference has one: {why}")

    rl = [p for p in ref.phdrs if p.type == PT_LOAD]
    nl = [p for p in new.phdrs if p.type == PT_LOAD]
    rp, np_ = [perms(p.flags) for p in rl], [perms(p.flags) for p in nl]
    if rp != np_:
        add("info", "segments", f"PT_LOAD segments are {' '.join(np_) or 'none'}; "
                                f"the reference has {' '.join(rp) or 'none'}")
    ra, na = max((p.align for p in rl), default=0), max((p.align for p in nl), default=0)
    if na and ra and na < ra:
        # a kernel or firmware image is placed by its boot loader, not mapped page by page
        booted = scripted and ref.interp is None
        add("info" if booted else "warn", "alignment",
            f"PT_LOAD segments are aligned to {na:#x}; the reference aligns them to {ra:#x}: "
            "on a system with larger pages they cannot be mapped")

    # what each section is loaded as. A permission the reference grants is only missed if the
    # section asks for it: GNU ld puts much of a kernel in one RWX segment, and loading
    # __ex_table read-only there loses nothing.
    rsec = {s.name: s for s in _placed(ref)}
    nsec = {s.name: s for s in _placed(new)}
    common = [n for n in rsec if n in nsec]
    lost_w, lost_x, got_x, got_w, lost_relro = [], [], [], [], []
    for n in common:
        a, b = _load_of(ref, rsec[n]), _load_of(new, nsec[n])
        if a is None or b is None:
            continue        # validate() reports an allocated section outside every segment
        if a.flags & PF_W and not b.flags & PF_W and nsec[n].flags & SHF_WRITE:
            lost_w.append(n)
        if a.flags & PF_X and not b.flags & PF_X and nsec[n].flags & SHF_EXECINSTR:
            lost_x.append(n)
        if b.flags & PF_X and not a.flags & PF_X:
            got_x.append(n)
        if b.flags & PF_W and not a.flags & PF_W:
            got_w.append(n)
        if (_inside(ref, rsec[n], PT_GNU_RELRO) and new.has_relro
                and not _inside(new, nsec[n], PT_GNU_RELRO)):
            lost_relro.append(n)
    if lost_w:
        add("error", "permissions", f"writable sections loaded read-only; the reference loads them "
                                    f"writable: {_names(lost_w)}")
    if lost_x:
        add("error", "permissions", f"code loaded without execute permission; the reference executes "
                                    f"it: {_names(lost_x)}")
    if got_x:
        add("warn", "permissions", f"mapped executable together with the code; the reference keeps "
                                   f"them out of executable segments (-z separate-code): {_names(got_x)}")
    if got_w:
        add("warn", "permissions", f"loaded writable; the reference loads them read-only: {_names(got_w)}")
    if lost_relro:
        add("warn", "relro", f"no longer read-only after relocation (outside PT_GNU_RELRO): "
                             f"{_names(lost_relro)}")

    # order and placement
    ro = [n for n in sorted(common, key=lambda n: rsec[n].addr)]
    no = [n for n in sorted(common, key=lambda n: nsec[n].addr)]
    moved = _moved(ro, no)
    if moved:
        add("warn" if scripted else "info", "order",
            f"{len(moved)} section(s) placed in another order than the reference: {_names(moved)}")
    rbase = min((p.vaddr for p in rl), default=None)
    nbase = min((p.vaddr for p in nl), default=None)
    if rbase is not None and nbase is not None and rbase != nbase and ref.type == 2:
        add("error" if scripted else "info", "base",
            f"image starts at {nbase:#x}; the reference starts at {rbase:#x}")
    shifted = [n for n in ro if rsec[n].addr != nsec[n].addr]
    if shifted and (scripted or ref.type == 2):
        first = shifted[0]
        add("info", "placement",
            f"{len(shifted)} section(s) at another address, from {first} on "
            f"({nsec[first].addr:#x}; the reference has {rsec[first].addr:#x})")
    lma = []
    for n in common:
        a, b = _load_of(ref, rsec[n]), _load_of(new, nsec[n])
        if a is not None and b is not None and (a.paddr - a.vaddr) != (b.paddr - b.vaddr):
            lma.append(f"{n} at {nsec[n].addr + b.paddr - b.vaddr:#x} "
                       f"(reference {rsec[n].addr + a.paddr - a.vaddr:#x})")
    if lma:
        add("error" if scripted else "warn", "lma",
            f"{len(lma)} section(s) loaded at another physical address (LMA): {_names(lma, 4)}")

    # sizes of individual sections (the total is elfcheck.compare's)
    grew = []
    for n in common:
        r, s = rsec[n].size, nsec[n].size
        if abs(s - r) > 4096 and abs(s - r) > r * 0.10:
            grew.append(f"{n} {s - r:+d} bytes ({(s - r) / r:+.0%})")
    if grew:
        add("info", "sizes", f"section(s) more than 10% larger or smaller: {_names(grew, 4)}")

    # boundary symbols
    rm, nm = _markers(ref), _markers(new)
    rplaced, nplaced = list(rsec.values()), list(nsec.values())
    off: List[str] = []
    off_linker: List[str] = []
    for name, value in rm.items():
        if name not in nm:
            continue
        edges = {(s, w) for s, w in _edges(rplaced, value) if s in nsec}
        if not edges or edges & _edges(nplaced, nm[name]):
            continue
        s, w = sorted(edges)[0]
        desc = f"{name} (the {w} of {s} in the reference)"
        (off_linker if name in LINKER_SYMBOLS and not scripted else off).append(desc)
    if off:
        add("error" if scripted else "warn", "boundaries",
            f"{len(off)} symbol(s) no longer mark the section edge they mark in the reference: "
            f"{_names(off, 4)}")
    if off_linker:
        add("info", "boundaries", f"linker-defined symbol(s) at another section edge: {_names(off_linker, 4)}")
    empty = []
    for a, b in _pairs(rm):
        if a in nm and b in nm and rm[b] > rm[a] and nm[b] <= nm[a]:
            empty.append(f"{a}..{b} ({rm[b] - rm[a]} bytes in the reference)")
    if empty:
        add("error", "ranges", f"{len(empty)} range(s) empty that hold data in the reference: "
                               f"{_names(empty, 4)}")
    return out
