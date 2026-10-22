# eldconv usage guide

Contents: [concepts](#concepts) · [workflow](#the-workflow) · [recipes per build system](#recipes) ·
[command reference](#command-reference) · [reading the report](#reading-the-report) ·
[environment variables](#environment-variables) · [CI](#using-eldconv-in-ci) ·
[troubleshooting](#troubleshooting)

## Concepts

**Conversion is a copy.** `eldconv convert SRC DST` never edits `SRC`. It copies the tree to `DST`,
rewrites linker flags and linker selection in the copy, and adds a few generated files. `DST` is
written atomically (a temporary sibling directory, then a rename), so an interrupted run leaves
nothing half-converted. `DST` must be outside `SRC` and its path must not contain whitespace.

**The shim.** `DST/eld-bin/ld` is a short shell script that execs your `ld.eld`. Compilers are pointed at
it (`-B<dir>/`, `COMPILER_PATH`, `LD=`, direct invocations rewritten), so every link passes through
it. The shim:

- logs each link command when `ELDCONV_LINK_LOG` is set (this is how `check` knows what was linked),
- removes GCC's `-plugin liblto_plugin.so` arguments, which eld cannot load,
- removes a few `-z` hints eld rejects and that are safe to lose (`-z ibt`, `-z shstk`,
  `-z cet-report=…`, `-z origin`),
- adds GNU ld's "supported targets" line to `--help`, because libtool otherwise builds only static libraries.

**The eld surface.** eld ignores unknown options with a warning. eldconv therefore filters every
flag against a snapshot of what eld accepts. The bundled snapshot is a recent eld (`main` of 13 Oct 2026,
which still reports itself as 24.0). If your eld differs, let eldconv probe it, or make a file yourself:

```sh
eldconv convert SRC DST --eld /opt/eld/bin/ld.eld --eld-surface auto   # probes once (1-2 min), cached in ~/.cache/eldconv
```

or

```sh
eldconv surface --eld /opt/eld/bin/ld.eld -o my-eld-surface.json
eldconv convert SRC DST --eld /opt/eld/bin/ld.eld --eld-surface my-eld-surface.json
```

The `auto` cache (`$XDG_CACHE_HOME/eldconv`, else `~/.cache/eldconv`) is keyed on the binary and on what
it prints for `--version`/`--help`, so a rebuilt eld behind a wrapper script is probed again. Probing
targets other than the host needs `clang`; without it only x86_64 is probed. With the default
(bundled) snapshot, `convert` warns when your ld.eld's `--help` lacks options the snapshot assumes,
i.e. your eld is older than the snapshot. `--eld-surface none` trusts the rule table alone.

**Severity.** A dropped option is either harmless (reported as `warn`) or loses an effect (reported
as `error`, e.g. `--icf=all`: the binary is no longer folded). `error` does not stop the conversion
unless you pass `--strict`.

## The workflow

```sh
eldconv detect  SRC                                  # 1. look
eldconv convert SRC DST --eld PATH                   # 2. convert
$EDITOR DST/eldconv-report.md                        # 3. review errors and warnings
eldconv check   DST --eld PATH                       # 4. build with eld, verify
```

Step 1 is optional but worth it: a tree with several build systems, or with conditional linker
selection (`ifneq ($(LLVM),)`), may need `--build-system` or `--source-linker`.

`detect` prints JSON: `system`, `toolchain` (gnu/llvm), `linker`, `conditional_linkers`,
`ambiguous`, and the `evidence` lines (`file:line: fuse -> lld`) it based that on.

## Recipes

### CMake

```sh
eldconv convert ~/src/proj ~/src/proj-eld --eld /opt/eld/bin/ld.eld
eldconv check   ~/src/proj-eld --eld /opt/eld/bin/ld.eld
# or build by hand
~/src/proj-eld/build-eld.sh                  # configure + build in proj-eld/build-eld
~/src/proj-eld/build-eld.sh /tmp/my-build    # another build directory
```

Extra CMake arguments go on both commands:

```sh
eldconv convert ~/src/proj ~/src/proj-eld --eld PATH --cmake-arg=-DCMAKE_BUILD_TYPE=Release --cmake-arg=-GNinja
eldconv check   ~/src/proj-eld --eld PATH --cmake-arg=-DCMAKE_BUILD_TYPE=Release --cmake-arg=-GNinja
```

Use the `=` form: argparse otherwise reads a value starting with `-` as another option.

If your project already uses a toolchain file, chain it: `--toolchain-file path/to/orig.cmake`.
`CMAKE_LINKER_TYPE`, `-fuse-ld=`, `--ld-path=`, `LINKER:` and `LINKER:SHELL:` forms, and
`CMakePresets.json` are handled.

### Make

```sh
eldconv convert ~/src/proj ~/src/proj-eld --eld PATH
make -C ~/src/proj-eld                       # entry-point makefiles include eld.mk
eldconv check ~/src/proj-eld --eld PATH      # same, plus verification
```

Pass targets and variables through `--build-arg` (repeatable):

```sh
eldconv check DST --eld PATH --build-arg zstd --build-arg lib-release --build-arg V=1
```

A bare word is a target; `VAR=VAL` is a make variable. Variables are also given to the internal
`make clean`; targets are not (otherwise `clean` would build them and hide the real link from the check).

Recursive makes (`$(MAKE) -C sub`) work: `eld.mk` exports `LD`, the `-B` flag and `COMPILER_PATH`
with absolute paths, so sub-makefiles that rebuild their own `LDFLAGS` still reach the shim.

Linux kernel / Kbuild trees are detected (`scripts/Kbuild.include`): `COMPILER_PATH` is then *not*
exported, so host tools (`fixdep`, `modpost`) keep using the host linker and only target links use eld.

### Cross builds and the Linux kernel

A cross build needs the same variables you would pass to `make` by hand; without them the check
silently builds for the host.

```sh
eldconv convert ~/src/linux ~/src/linux-eld --eld PATH
eldconv check ~/src/linux-eld --eld PATH --jobs 8 --timeout 3600 \
    --pre-build "make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig &&
                 ./scripts/config --disable RANDOMIZE_BASE &&
                 make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig" \
    --build-arg ARCH=arm64 --build-arg CROSS_COMPILE=aarch64-linux-gnu- --build-arg vmlinux
```

Turn KASLR (`CONFIG_RANDOMIZE_BASE`, on in arm64 `defconfig`) off: the kernel links, but eld sets the
linker-script symbols around `.rela.dyn` (`__pi_rela_start`/`__pi_rela_end`) as if the section were
empty, so the kernel applies none of its relocations and crashes before printing anything once KASLR
moves it. `CONFIG_RELOCATABLE` without KASLR boots. `convert` warns about this on kernel trees, and
`check` reads the bounds in the linked `vmlinux`: it fails a KASLR kernel and only warns otherwise.
The same check fails any other image (a boot loader, say) whose linker script brackets its
dynamic relocations the same way.

Notes: eld builds before late September 2026 cannot link `-m elf_i386`, so x86 kernels (whose
real-mode trampoline is i386) fail there. Recent eld links i386, and the vDSO and `realmode.elf` then
go through eld, but the final x86 `vmlinux` link still fails on an eld program-header error
(`Fatal: Ignoring physical address for section .rodata which is already part of PHDR text`); use
arm64, arm or riscv. `convert` warns about this on every kernel tree, since every tree carries
`arch/x86`; ignore it when you build another `ARCH`. The kernel reads the linker version string:
`GNU ld compatible linker - eld 24.0`.

### Autotools

```sh
eldconv convert ~/src/proj ~/src/proj-eld --eld PATH
eldconv check   ~/src/proj-eld --eld PATH \
    --build-arg=--without-ssl --build-arg=--prefix=/opt/x
```

For Autotools, `check` runs `autoreconf -fi` if there is no `configure`, then
`./configure LDFLAGS=… <options>`, patches libtool so `-B` reaches shared-library links, then
`make`. Options and `VAR=VAL` go to `configure`; bare words go to `make`.
`configure.ac`, `Makefile.am` and `Makefile.in` are rewritten.

### Hand-written `./configure` + `Makefile.in` (zlib style)

If a project's own (non-autoconf) `configure` generates the Makefile, tell eldconv to treat it as Make
and run configure first:

```sh
eldconv convert ~/src/zlib ~/src/zlib-eld --eld PATH --build-system make
eldconv check   ~/src/zlib-eld --eld PATH --pre-build ./configure
```

`--pre-build CMD` runs in the converted tree before the build and is repeatable.
`Makefile.in` at the tree root is treated as an entry-point makefile.

### Meson

```sh
eldconv convert ~/src/proj ~/src/proj-eld --eld PATH
eldconv check   ~/src/proj-eld --eld PATH --build-arg=-Dfoo=enabled --build-arg mytarget
```

The link arguments are injected right after `project()` in `meson.build`; native/cross files with
`c_ld`/`cpp_ld` are converted. Options go to `meson setup`, bare words to `meson compile`.

### GN (Chromium-style)

```sh
eldconv convert ~/src/chromium ~/src/chromium-eld --eld PATH
eldconv check   ~/src/chromium-eld --eld PATH --build-dir out/eld --build-arg chrome
```

lld-only flags come out of the `.gn`/`.gni` files, `use_lld`/`use_mold` are pinned off in
`eld-args.gn`, and the shim is spliced into each `tool("link")`. Windows, Apple and z/OS toolchains are
skipped. `check` appends `eld-args.gn` to `out/eld/args.gn` and runs `gn gen` and `ninja`.

### Standalone Ninja

A hand-written or generator-emitted `build.ninja` is converted in place in `DST`: flags in `ldflags`
variables and link-rule commands are translated, and `-B<shim>/` is spliced after the driver of
each link rule (`link`, `solink`, …; archivers are left alone). A link rule whose command does not
start with a recognisable driver (a Python wrapper, say) is reported with the flag to add by hand.

```sh
eldconv check DST --eld PATH --build-arg mytarget
```

### Just converting flags

```sh
$ eldconv flags "-Wl,--icf=all -Wl,-z,now -Wl,--gc-sections" --source-linker lld
[error] --icf=all: dropped, semantics lost: eld has no identical-code-folding; binary size may grow
-Wl,-z,now -Wl,--gc-sections

$ eldconv flags --raw --source-linker bfd "--relocatable -o x.o a.o"
[info] --relocatable: renamed to `-r`; eld spells this -r
-r -o x.o a.o
```

Diagnostics go to stderr, the converted flags to stdout.

## Command reference

### `eldconv detect SRC`

`--source-linker {bfd,lld,gold,mold}` · `--build-system {cmake,make,gn,ninja,meson,autotools}` · `-v`

### `eldconv convert SRC DST`

| option | meaning |
|---|---|
| `--eld PATH` | ld.eld to use; default is `ld.eld` on `PATH`. The path is baked into the shim; override at build time with `ELDCONV_ELD`. |
| `--source-linker L` | force the source linker (also selects which conditional branch to convert) |
| `--build-system S` | force the build system |
| `--toolchain-file F` | CMake: original toolchain file to chain |
| `--cmake-arg A` | CMake: extra configure argument, repeatable |
| `--eld-surface X` | `bundled` (default), `auto` (probe `--eld`, cache it), a file from `eldconv surface`, or `none` |
| `--force` | replace `DST`; refused unless `DST` is empty or contains `eldconv-report.json` |
| `--strict` | exit 1 if the conversion reported any `error` diagnostic |
| `--check` | run `check` afterwards; it takes every `check` option below except `--eld`, `--cmake-arg` (shared with the conversion) and `--dry-run` |
| `-q` | do not print diagnostics (snapshot warnings and `auto` progress still go to stderr) |
| `-v`, `-vv` | explain decisions / list every file and flag |

### `eldconv check DST`

| option | meaning |
|---|---|
| `--eld PATH` | ld.eld (default: the one recorded at conversion, then `PATH`) |
| `--build-arg ARG` | extra build argument, repeatable; see the recipes for how each build system uses it |
| `--pre-build CMD` | shell command run in `DST` before the build, repeatable |
| `--build-dir DIR` | out-of-tree build directory; relative paths are relative to `DST`. It is deleted before a fresh configure **only** if it is inside `DST` or is a recognisable CMake/Meson build directory |
| `--cmake-arg A` | CMake configure argument, repeatable |
| `--dry-run` | print the commands without running them |
| `--no-clean` | incremental build; an up-to-date tree then verifies nothing |
| `--no-probe` | skip validating flags against `ld.eld --help` |
| `--allow-unresolved-data` | report binaries with undefined data symbols and no relocation as warnings instead of failures |
| `--allow-ignored-version-scripts` | report version scripts eld skipped as warnings instead of failures |
| `--compare` | also build the original tree with its own linker and report how each eld output differs (below) |
| `--reference-dir DIR` | where `--compare` builds the original (default: `DST.reference`); an existing directory is only replaced if `--compare` made it |
| `--timeout S` | per-command timeout; it stops the whole build, not only its top command |
| `-j N` | parallel jobs (default: the number of CPUs) |

Results are printed as JSON and saved to `<build dir>/eldconv-check.json`. Every command's full
output is in `<build dir>/eldconv-build.log` (printed as it arrives with `-v`; the JSON keeps only
the last 4000 characters). The shim log is `<build dir>/eld-link.log`.

Every output is also checked as an image: section and program headers inside the file, `PT_LOAD`
segments aligned and not overlapping, the entry point inside an executable segment, every allocated
section inside a loaded segment, a `PT_INTERP` path and a dynamic string table where they are
needed, symbols that point at real sections. A failure means a broken image whatever linked it.
Writable+executable segments, an executable stack and text relocations are warnings.

**`--compare`.** The original tree (the `source` recorded in `eldconv-report.json`) is copied to
`DST.reference` and built there with its own linker: the same `--pre-build` commands and
`--build-arg`s, without the shim. Each eld output is then compared with the file of the same path
in that build:

| difference | level |
|---|---|
| ELF class, machine or type; `PT_INTERP`; `DT_SONAME` | error |
| a library no longer needed (`DT_NEEDED`) | error |
| a symbol no longer exported, or exported with another version | error |
| RELRO, BIND_NOW or PIE lost; an executable stack gained | error |
| a library newly needed; a symbol newly exported or newly undefined | warning |
| `RUNPATH`; text relocations; build-id; entry symbol | warning |
| an allocated section or global symbol missing | warning |
| more than 10% (and 64 KiB) larger or smaller | warning |
| sections and symbols linkers make up for themselves; size | info |

Errors fail the check, warnings are listed, and every difference is recorded per output in
`eldconv-check.json` (`differences`, next to `reference`). The reference build's output is in
`DST.reference/<build dir>/eldconv-reference-build.log`.

### `eldconv compare REF OUT`

Validates `OUT` and compares it with `REF` as `check --compare` does, for two files you built
yourself (say `ld.bfd`'s and eld's link of the same objects). Prints JSON; exit 1 if any error.

### `eldconv flags "FLAGS"`

| option | meaning |
|---|---|
| `--source-linker L` | the linker the flags were written for (default `bfd`) |
| `--raw` | the input is a bare `ld` argv, not compiler-driver flags (`-Wl,…`) |
| `--eld-surface X`, `--eld PATH` | as for `convert` |

### `eldconv surface --eld PATH [-o FILE] [-j N]`

Links small objects with each option and `-z` keyword for x86_64, aarch64, arm, riscv32, riscv64,
i386 and hexagon, and records what eld accepts. A target eld cannot link at all (i386 on an eld older
than late September 2026) is left out of the snapshot. Takes a minute or two; needs `clang` for the
non-host targets.

### `eldconv audit --eld PATH`

For maintainers: reports where the mapping table disagrees with a real eld: `missing` (kept but not
accepted), `needless` (dropped but supported), `confirmed`, `unknown` (eld options the table has no
rule for). Options eld accepts only for ARM/AArch64 are probed on those targets; PE/COFF passthrough
rules and the deliberate `-O` drop are not reported. Exit code 1 if anything is `missing`.

### Exit codes

| code | meaning |
|---|---|
| 0 | success (`check`: `CHECK PASSED`) |
| 1 | `check` failed, or `convert --strict` found an effect-losing drop |
| 2 | usage, input or conversion error (bad argument, `ld.eld` not found, source not a directory, `DST` exists without `--force`, …) |
| 130 | interrupted |

## Reading the report

`DST/eldconv-report.md` (and `.json`) list rewritten files, generated files and one line per
decision:

```
- [error] CMakeLists.txt:4: `--icf=all` — dropped, semantics lost: eld has no identical-code-folding; binary size may grow
- [warn]  CMakeLists.txt:4: `-fuse-ld=lld` — dropped: linker selection replaced by eld shim
- [info]  lib/Makefile:132: `--out-implib …` — PE/COFF (Windows) only; left untouched
```

| level | meaning | what to do |
|---|---|---|
| `info` | renamed, kept tentatively, or deliberately left alone | nothing; `check` confirms tentative ones |
| `warn` | a flag was dropped and nothing is lost, or something to be aware of (i386 links on an eld without an i386 backend, x86 kernel trees) | skim |
| `error` | an effect is lost, or something cannot be converted (response file generated at build time, `kbuild-ld-name` branches) | decide per item |

Typical `error` items: `--icf=*` (size grows), `-z pack-relative-relocs` (no RELR),
`--compress-debug-sections` (debug info is larger), a linker chosen in a CI script or container
(the converter tells you it cannot change that).

## Environment variables

| variable | used by | meaning |
|---|---|---|
| `ELDCONV_ELD` | shim, `build-eld.sh` | path to ld.eld at build time (overrides the baked-in path) |
| `ELDCONV_LINK_LOG` | shim | file that receives one record per link (set by `check`) |
| `ELDCONV_KEEP_GCC_PLUGIN` | shim | do not strip GCC's LTO plugin arguments |
| `ELDCONV_KEEP_Z_HINTS` | shim | do not strip `-z ibt/shstk/cet-report/origin` |
| `ELDCONV_TEST_ELD` | tests | run the e2e tests against a real ld.eld |
| `XDG_CACHE_HOME` | `--eld-surface auto` | where probed snapshots are cached (`$XDG_CACHE_HOME/eldconv`, default `~/.cache/eldconv`) |
| `JOBS` | `build-eld.sh` | parallelism |

## Using eldconv in CI

```sh
eldconv convert . ../proj-eld --eld "$ELD" --strict --quiet   # fail if a flag with an effect is lost
eldconv check ../proj-eld --eld "$ELD" --timeout 3600          # fail if eld did not link everything
```

Cache `$ELD`; pass `--eld-surface auto` (or regenerate your own `eldconv surface` snapshot) whenever the eld you use changes.
This repository's own workflows are `.github/workflows/ci.yml` (lint and tests against a stand-in
eld, on every change) and `.github/workflows/nightly.yml` (builds eld `main` on llvm-project `main` with clang,
saves `ld.eld` as an artifact, then converts a Linux kernel with it, checks that the arm64 `Image` links through
eld and boots it under qemu; the helpers are in `ci/kernel/`).

## Troubleshooting

**`no link step went through eld (shim log empty)`**: the build ignored every selection mechanism.
Run `eldconv check DST -v` and look at the commands; check whether the build calls the linker by
absolute path or from a script outside the build files (the converter warns about those). A relative
shim path in a hand-edited makefile is a common cause: use absolute paths.

**`eld did not recognise, and silently ignored, these flags`**: a flag reaches eld that eld does not
know. If it comes from the build at run time, add it to the shim filter or fix the build; if it is in
a file, report it with `eldconv flags`.

**`undefined data symbol(s) with no relocation`**: the produced executable would crash. This is the eld
x86_64 copy-relocation limitation (still present in current eld). Use an aarch64/arm/riscv target, or
`--allow-unresolved-data` to check the rest.

**`eld ignored the named version nodes of these version scripts`**: this ld.eld was built without
`-DELD_ENABLE_SYMBOL_VERSIONING=ON` (the CMake default is OFF), so a `zlib.map`-style script
(`ZLIB_1.2 { global: ...; local: *; };`) is skipped: no symbol versions, every symbol exported.
Rebuild eld with that option, or pass `--allow-ignored-version-scripts` to check the rest.

**`the build failed before any real link`**: configure or an early build step failed (a missing
dependency, say). If eld ran for configure-time probe links, linker selection works; read
`stderr_tail` in the report.

**`N ELF file(s) were produced without going through eld`**: a warning. Host tools, copied or stripped
outputs are expected here. A *problem* is only raised if another linker's signature is in the file.

**`--build-arg: expected one argument`**: write `--build-arg=--prefix=/x` (with `=`) when the value
starts with `-`.

**Libraries come out static only (Autotools)**: libtool decided eld cannot make shared libraries.
The generated shim handles this; make sure `configure` runs with `COMPILER_PATH`/`LDFLAGS` set by
`check` or `build-eld.sh` rather than a bare `./configure`.

**`LD := ld` used before `eld.mk`**: handled; the rewritten assignment falls back to the shim path
until `eld.mk` defines it.

**Check hangs or is slow**: run it with `-v` to watch the build, or follow
`<build dir>/eldconv-build.log`; use `--timeout`, `--jobs`, and `--build-arg <target>` to build
something narrower than the default target.
