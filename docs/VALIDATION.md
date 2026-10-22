# Validation notes

How eldconv was checked against real projects and real `ld.eld` builds, and what that found. Every
eld build used reports itself as 24.0: an earlier `main` build, and `main` of 13 Oct 2026 (972e9fa).

## Chromium (GN/Ninja)

`eldconv` handles GN because Chromium does. On Chromium's real `build/` tree it
reports **gn / llvm / lld** and rewrites 3 files: the lld-only flags come out of
`build/config/compiler/BUILD.gn`, and `-B<shim>/` is spliced into the three
`link_command = "$ld ..."` lines in `build/toolchain/gcc_toolchain.gni`.

Selection goes into `tool("link")`, not into `ldflags` or `extra_ldflags`: GN
escapes spaces inside a string arg into a single token, and `extra_ldflags` means
different things in different projects. The Windows, Apple and zOS toolchains are
skipped — their link drivers do not take `-B`, and eld targets ELF.

A full Chromium build needs a checkout and toolchain far larger than this
validation, so the end-to-end GN build was done on a project shaped like
Chromium's config (same ldflags, same `tool("link")` structure, real `gn` +
`ninja`): both the executable and the shared library link through eld and run.


## Linux kernel

`eldconv` converts a GCC kernel tree (v7.3-rc3, x86_64 defconfig) in ~40s: 9 of
3437 build files change and nothing else is touched. With a stub `ld.eld` (a wrapper over GNU ld)
the tree then links `vmlinux`, the vDSOs, realmode.elf, setup.elf and every `.ko` through the shim.
With real eld, x86 stops in eld (see below).

Verified with **real eld**: an arm64 `tinyconfig` kernel converts, builds and
links through eld (3 real links: the vDSO, `vmlinux.o` and `vmlinux.unstripped`). The kernel
records the linker in its own version string: `GNU ld compatible linker - eld 24.0`.

| eld | `check` |
|---|---|
| earlier eld build (before the i386 backend) | passed (`check` did not yet look for skipped version scripts) |
| `main` of 13 Oct 2026, default CMake options | fails only on `vdso.lds`: eld skips its `LINUX_2.6.39` node |
| same, `-DELD_ENABLE_SYMBOL_VERSIONING=ON` | passes; the vDSO exports `__kernel_*@@LINUX_2.6.39` |

- `scripts/ld-version.sh` accepts only a linker whose `--version` says `GNU ld` or
  `LLD` — the gate that stops mold. **eld passes it unmodified**, because it
  reports `GNU ld compatible linker - eld 24.0`, which kbuild reads as `BFD 24.0`.
  `convert` still flags the gate for other linkers and other eld builds.
- **x86 does not work**. eld builds before late September 2026 cannot link `-m elf_i386` at all
  (minimal repro: `ld.eld -m elf_i386 -shared -o o.so a32.o` → `Fatal: Linking had errors`), and
  x86's realmode trampoline is always i386. eld `main` of 13 Oct 2026 links the i386 parts, then
  stops at `vmlinux.unstripped`: `Fatal: Ignoring physical address for section .rodata which is
  already part of PHDR text`.
- `--pic-veneer` (arm/arm64 Makefiles) was silently ignored by the earlier eld and reported by
  `convert` as an error; the current eld implements it, and it is kept.
- eld's plain `--help` is its host (x86_64) driver's. `--pic-veneer` only appears in
  `ld.eld -m aarch64elf --help`, so `check` now reads the help of every emulation the links used.

### Booting it: arm64 KASLR (20 October 2026)

eld `main` (79e4f37, on llvm-project a1c9008, built with clang 18 and
`-DELD_ENABLE_SYMBOL_VERSIONING=ON`), Linux v6.12, `tinyconfig` plus `ci/kernel/boot.config`,
`eldconv check`, then booted on `qemu-system-aarch64 -M virt` with `ci/kernel/boot.sh`:

| config | linker | `check` | boot |
|---|---|---|---|
| `RELOCATABLE` off | eld | passes | reaches init |
| `RELOCATABLE=y` | eld | passes, warns | reaches init |
| `RELOCATABLE=y`, `RANDOMIZE_BASE=y` | eld | fails | hangs with no output |
| `RELOCATABLE=y`, `RANDOMIZE_BASE=y` | GNU ld 2.46 | — | reaches init, `KASLR enabled` |

The arm64 linker script brackets the relocations the kernel applies to itself:
`.rela.dyn : { __pi_rela_start = .; *(.rela .rela*) __pi_rela_end = .; }`. eld fills `.rela.dyn`
with the relocations it generates (4681 `R_AARCH64_RELATIVE`, 0x1b6d8 bytes) but sets both symbols
to its start address. The kernel then applies none of them. That is harmless while the kernel runs
where it was linked, and fatal once KASLR moves it. GNU ld's `__pi_rela_end` is the section's end.

- The flags `convert` drops for arm64 are not the cause. `--no-apply-dynamic-relocs` and RELR are
  both behind `ld-option`. Dropping the first is why the relocatable kernel without KASLR boots:
  eld writes the final values in place, so nothing needs relocating.
- `check` now fails an image whose `X_start`/`X_end` pair does not span a non-empty
  `.rela.dyn`/`.rel.dyn`/`.relr.dyn`, and only warns for a kernel built without
  `CONFIG_RANDOMIZE_BASE`. `convert` warns about KASLR on every kernel tree that has it.
- eld's own kernel boot workflow (`linux-kernel-run.yml`) also turns off `RANDOMIZE_BASE` and
  `RELOCATABLE` for arm64.


### Re-validation with eld `main` of 13 October 2026

eld `main` (972e9fa) still reports `eld 24.0`. New options found in its history and now in the
mapping and the bundled snapshot: `--pic-veneer` (ARM/AArch64), `--[no-]undefined-version`,
`--[no-]merge-exidx-entries`, `--no-merge-constants`, `--replay`, and generic `-O<n>` (accepted and
ignored). New targets in the snapshot: riscv32 and i386.

- x86_64 kernel: the vDSO i386 link and `realmode.elf` now go through eld. The build then stops
  at `vmlinux.unstripped` with an eld Fatal about program headers and physical addresses. This is
  in eld, not in the conversion.
- arm64 kernel: see the table above.
- Real projects, `check --allow-unresolved-data` (x86_64 has no copy relocations), default eld build:

  | project | build system | result |
  |---|---|---|
  | lz4, zstd, lua | Make | pass |
  | fmt, libuv | CMake | pass |
  | jq | Autotools | pass |
  | curl | CMake (`-DCURL_USE_LIBPSL=OFF`), Autotools (`--without-libpsl --without-ssl`) | pass |
  | zlib | CMake, Make (`--pre-build ./configure`) | fails only on the skipped `zlib.map`; CMake build checked with a symbol-versioning eld: passes |
- An eld built without `-DELD_ENABLE_SYMBOL_VERSIONING=ON` (the CMake default is OFF) skips every
  named version node ("Only anonymous version nodes are supported"): no symbol versions, and the
  nodes' `local:` hiding is lost. `check` now reports this as a problem. Build eld with that option
  for libraries that use version scripts. Verified on a zlib-style `ZLIB_1.2.0 { global: zlibVersion;
  local: *; };`: the default build exports every symbol unversioned; a `-DELD_ENABLE_SYMBOL_VERSIONING=ON`
  build emits `zlibVersion@@ZLIB_1.2.0` and hides the rest.
- zlib (CMake) with the new eld: all 8 links go through eld; `check` now flags `zlib.map` as an ignored
  version script and the executables that use `stdout`/`stderr` (copy relocations).
  With a `-DELD_ENABLE_SYMBOL_VERSIONING=ON` eld, `libz.so` carries `ZLIB_1.2.0`... versions and
  `check --allow-unresolved-data` passes.
- x86_64 still has no copy relocations, so the `unresolved data symbol` check stays.
- `eldconv audit` against this eld: 0 missing, 0 needless (after teaching it that `--pic-veneer`,
  `--fix-cortex-a53-843419` and `--[no-]merge-exidx-entries` are accepted only for ARM/AArch64).
- A snapshot older or newer than the eld in use is detected (`--eld-surface auto` probes and caches).

## Auditing the table against a real linker

```
eldconv audit --eld /path/ld.eld
```

Probes the linker and reports where the mapping table disagrees with it:
`missing` (a kept option this eld does not accept), `needless` (an option the
table drops that eld supports), `confirmed` (a tentative rule the probe agrees
with) and `unknown` (options eld has that the table has no rule for).

The probe links a real object rather than asking `--version`: **eld does not
fail on an option it does not know** — it prints `Warning: Unrecognized option`
and links anyway. `eldconv check` scans the build output for those warnings and
fails the check, because otherwise a build "succeeds" with flags silently doing
nothing.

A `-z <keyword>` rule is keyed on `(name, value)`, not just the flag name: a
bare `-z` being in `--help` says nothing about which keywords eld accepts (its
`-z` is one generic entry covering dozens of keywords), so each keyword gets
its own probe — `-z <keyword>` in a real link — rather than every `-z` rule
collapsing onto whichever one the table lists first.

Running it against ld.eld 24.0 corrected 40-odd rules, including: eld spells its
LTO option `--flto-options` (not `--lto-options`); it *does* support
`--no-warn-rwx-segments`, `--print-memory-usage`, `--thinlto-jobs`,
`--unresolved-symbols`, `-plugin`/`-plugin-opt`, `--lto-emit-asm`,
`--lto-emit-llvm`, `--lto-partitions=N`, `--no-warn-mismatch`, `--warn-once`,
and the *undocumented* `-z nognustack`/`-z noseparate-code` (eld's own `--help`
only advertises the hyphenated `no-separate-code`, which eld itself then
rejects); and it does *not* have `--no-warn-common`, `--no-print-gc-sections`,
`--relocatable`, `--unique`, `--retain-symbols-file`, `-z undefs`, `-z textoff`,
`-z origin`, or `-dT`/`--dT` (renamed to its accepted `--default-script=`
synonym instead of being dropped). `--icf` really is absent, as the table
assumed.


## Bugs found by validating against real eld

Re-validating the whole pipeline against the real, self-built `ld.eld` (not
just the fake stub used for fast unit tests) surfaced two checker bugs where a
build that had genuinely linked everything through eld was still reported as
`CHECK FAILED`:

- **A relative `--build-dir` resolved against the wrong directory.** `run_check`
  used a user-supplied relative build dir (e.g. `--build-dir out/eld`, exactly
  as `eldconv`'s own generated `build-eld.sh` uses it for GN) as-is, instead of
  anchoring it under the project being checked. `gn`/`ninja` still built
  correctly (they run with `cwd=DST`), but the shim's `ELDCONV_LINK_LOG` env
  var, and the path `eldconv` itself later inspected, pointed at a directory
  next to wherever the `eldconv` *process* happened to be run from. Every real
  link happened and eld genuinely performed them, but the log recording that
  fact was never seen. Fixed by resolving `build_dir` against `dst` up front.
- **`make clean` silently also built the target.** `--build-arg` is meant for
  `VAR=VAL` build variables (`ARCH=arm64 CROSS_COMPILE=...`), but a target name
  passed the same way (`--build-arg vmlinux`, needed to build only `vmlinux`
  rather than kbuild's much slower default target) was echoed onto the internal
  `make clean` step too, turning it into `make clean vmlinux`. A real `make`
  treats that as "clean, then also build vmlinux" — so the real eld link
  happened, and was logged, *during the clean phase* — and the log is then
  deleted (to reset it for "the real build") before the nominal build command
  runs, which now finds vmlinux already up to date and links nothing. Net
  effect: a kernel that fully linked through eld was reported as CHECK FAILED
  ("eld ran but performed no link"). Fixed by only forwarding `VAR=VAL`-shaped
  `build_args` to the clean step.

Both were caught by re-running the real arm64 kernel validation with the
options a real user would reach for (`eldconv check --build-dir out/eld` for a
GN checkout, `--build-arg vmlinux` for a kernel), not by the original,
narrower validation run — a reminder that the checker's own correctness needs
the same real-linker treatment as the mapping table does.



Found when re-validating against eld `main` of 13 Oct 2026:

- **Target-only options reported as unknown.** `check` compared link flags with eld's plain
  `--help`, which is the x86_64 driver's, so the arm64 kernel's `--pic-veneer` was listed as "not
  listed by `ld.eld --help`". `check` now also reads `ld.eld -m <emulation> --help` for every
  emulation the links used.
- **Feature probes in the unknown-flags warning.** kbuild's `ld-option` offers
  `--pack-dyn-relocs=relr` to find out whether the linker has it; that probe link no longer feeds
  the warning.
- **Misleading message when configure failed.** curl without libpsl failed in `cmake`, after
  `try_compile` links through eld, and `check` said "build was up to date?". It now says the build
  failed before any real link and that eld is selected.
- **Skipped version scripts went unnoticed.** An eld built with default CMake options drops every
  named version node with a warning; the arm64 vDSO and zlib then link without symbol versions.
  `check` now fails on it (`--allow-ignored-version-scripts` to downgrade).


## Comparing eld's outputs with GNU ld's (21 October 2026)

`check --compare` against eld `main` bc23b56 (llvm-project ba448bd, the nightly's build), x86_64,
with `--allow-unresolved-data` for the known copy-relocation gap:

| project | outputs compared | differences beyond information |
|---|---|---|
| lz4 1.10.0 (Make) | `liblz4.so`, `lz4` | extra `DT_NEEDED`; `_GLOBAL_OFFSET_TABLE_` undefined; `main` exported |
| zlib 1.3.1 (Make, `--pre-build ./configure`) | `libz.so` and 4 programs | the same three |
| lua 5.4.7 (Make, `--build-arg linux`) | `lua`, `luac` | extra `DT_NEEDED`; `_GLOBAL_OFFSET_TABLE_` undefined |

All three are eld behaviour, confirmed with `readelf`:

- **Extra `DT_NEEDED` entries.** gcc links with `--push-state --as-needed -lgcc_s --pop-state`, and
  libc's `libc.so` script names `ld-linux-x86-64.so.2` in `AS_NEEDED(...)`. GNU ld records neither
  library when nothing uses it, and eld records both, so every eld output loads `libgcc_s.so.1`.
- **`_GLOBAL_OFFSET_TABLE_` is an undefined dynamic symbol** in eld's shared libraries and
  executables. GNU ld defines it locally.
- **`main` is exported** from eld's executables (`lz4`, zlib's examples) without `-E`.

All 9 outputs pass the image checks. Before tuning, the comparison also reported copy-relocated
`stdout`/`stdin`/`stderr` as lost exports. Those are the unresolved-data gap that `check` already
reports. It also reported `.data.rel.ro` against `.data.rel.ro.local`, tree paths in `RUNPATH`, and
page-sized growth of small programs; none of these are differences now. Validation alone, run over
6065 ELF files from this host's `/usr`, reports nothing.

### Layout (22 October 2026)

Comparing layouts as well (`layout.py`, now part of every `check`) finds three more differences in
the same 9 outputs:

- **No `PT_GNU_STACK`** (error). eld writes the segment only for an explicit `-z noexecstack` or
  `-z execstack`; it ignores the inputs' `.note.GNU-stack` sections, from which GNU ld decides. Without
  the segment the loader assumes an executable stack (on x86_64; glibc's default is non-executable on
  AArch64 and RISC-V). On this host's glibc 2.43, `dlopen` of eld's `liblz4.so` and `libz.so` failed
  with "cannot enable executable stack as shared object requires", and GNU ld's builds of the same
  libraries load. A minimal `-shared` link through eld gets `GNU_STACK RW` with `-Wl,-z,noexecstack`,
  and none without it. The shim now makes GNU ld's decision (bfd/elflink.c: an executable note, or a
  missing note on a target whose `elf_backend_default_execstack` is 1, means an executable stack) and
  passes it to eld: after reconverting, lz4, zlib (CMake) and lua get `GNU_STACK RW` on every output,
  `liblz4.so` loads, and `check` passes. Deciding costs about 14 ms per link.
- **No `PT_GNU_PROPERTY`** (warning): `.note.gnu.property` is not carried over, so the inputs' CET
  markings are lost.
- **Read-only data in the code segment** (warning): eld maps the headers, `.rodata`, `.eh_frame` and
  the dynamic tables in one `R X` segment with `.text`. GNU ld (`-z separate-code`, its x86_64 Linux
  default) keeps code in a segment of its own.

Section order and addresses differ too (eld places `.gnu.hash` and `.eh_frame_hdr` elsewhere); for
images a linker script does not place, that is only information.

