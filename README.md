# eldconv

[![ci](https://github.com/shaktisuman/eldconv/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/shaktisuman/eldconv/actions/workflows/ci.yml?query=branch%3Amain)
[![tests](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fshaktisuman%2Feldconv%2Fbadges%2Ftests.json&cacheSeconds=300)](https://github.com/shaktisuman/eldconv/actions/workflows/ci.yml?query=branch%3Amain)
[![nightly](https://github.com/shaktisuman/eldconv/actions/workflows/nightly.yml/badge.svg?event=schedule)](https://github.com/shaktisuman/eldconv/actions/workflows/nightly.yml?query=event%3Aschedule)

Convert a build that links with **bfd, lld, gold or mold** into a copy that links with
**[Qualcomm eld](https://github.com/qualcomm/eld)**, then build it and verify that eld really
performed every link.

Supported build systems: **CMake, Make, GN, Ninja, Meson, Autotools** (GCC or Clang toolchains).

```
your project ──eldconv convert──▶ converted copy ──eldconv check──▶ "eld linked N binaries" / list of problems
 (bfd/lld/gold/mold)                (links with eld)
```

The source tree is never modified. Everything is written to a new directory.

## Why not just swap the linker?

- eld **does not fail on options it does not know**: it prints `Warning: Unrecognized option`
  and links anyway. A naive swap "works" while silently ignoring flags. eldconv knows what eld
  accepts (empirically, by probing a real ld.eld) and renames, keeps, or drops each flag with a
  severity: *harmless* (warning) or *effect lost* (error).
- Selecting the linker is different in every build system and several mechanisms can override
  each other. eldconv installs a small shim and selects it in every way that applies.
- "The build passed" does not prove eld linked anything. `eldconv check` does.

## Install

Python 3.12+, no runtime dependencies.

```sh
git clone https://github.com/shaktisuman/eldconv
cd eldconv
pip install -e .            # installs the `eldconv` command
# or run without installing:
PYTHONPATH=. python -m eldconv --help
```

You need an `ld.eld` binary (build it from the eld repository, or use one from your toolchain).
For shared libraries with version scripts, build eld with `-DELD_ENABLE_SYMBOL_VERSIONING=ON`.
Pass it with `--eld /path/to/ld.eld`, or put it on `PATH`.

## Quick start

```sh
# 1. What does this tree use?
eldconv detect ~/src/myproj

# 2. Convert it (writes a new directory, never touches the source)
eldconv convert ~/src/myproj ~/src/myproj-eld --eld /opt/eld/bin/ld.eld

# 3. Read what was decided
less ~/src/myproj-eld/eldconv-report.md

# 4. Build with eld and verify (clean build, then checks every link)
eldconv check ~/src/myproj-eld --eld /opt/eld/bin/ld.eld
```

or all at once: `eldconv convert SRC DST --eld PATH --check`.

`check` prints a JSON report and ends with `CHECK PASSED` / `CHECK FAILED` (exit code 0 / 1).

To build the converted tree yourself instead, use the generated script:

```sh
~/src/myproj-eld/build-eld.sh            # CMake: configure + build in build-eld/
```

See **[docs/USAGE.md](docs/USAGE.md)** for per-build-system recipes (CMake, Make, Autotools with
a hand-written `configure`, Meson, GN, Ninja, cross builds such as the Linux kernel), every option,
the report format, CI integration and troubleshooting.

## Commands

| command | purpose |
|---|---|
| `eldconv detect SRC` | print build system, toolchain, source linker and the evidence (JSON) |
| `eldconv convert SRC DST` | write the eld-linked copy; `--check` builds and verifies it |
| `eldconv check DST` | clean build of the converted tree through the shim, then verify |
| `eldconv flags "<flags>"` | convert a flag string, e.g. `-Wl,--icf=all -Wl,-z,now`; `--raw` for a bare `ld` argv |
| `eldconv surface --eld PATH -o FILE` | snapshot which options *your* eld accepts |
| `eldconv audit --eld PATH` | compare the mapping table with a real ld.eld (maintainers) |

Every command takes `-v` (decisions and why) and `-vv` (every file and flag).
Progress goes to stderr, so JSON on stdout stays machine-readable.

Common `convert` options:

| option | meaning |
|---|---|
| `--eld PATH` | the ld.eld to use (default: `ld.eld` on `PATH`) |
| `--check` | run the checker after converting |
| `--strict` | exit 1 if any flag with a real effect had to be dropped (for CI) |
| `--source-linker {bfd,lld,gold,mold}` | override detection (also picks which branch of a conditional to convert) |
| `--build-system {cmake,make,gn,ninja,meson,autotools}` | override detection when a tree carries several |
| `--eld-surface FILE\|auto\|bundled\|none` | which eld option snapshot to filter with (`auto` probes your `--eld` once and caches it) |
| `--force` | replace `DST`, only if it is empty or a previous eldconv output |
| `--cmake-arg ARG` | extra argument for the generated CMake configure step (repeatable) |

## What gets generated

| file | build system | purpose |
|---|---|---|
| rewritten build files | all | linker flags translated; `-fuse-ld=`, `--ld-path=`, `CMAKE_LINKER_TYPE`, `LD=ld.xxx`, `mold -run` removed |
| `eld-bin/ld` | all | shim: logs the link command, strips GCC LTO plugin arguments, execs `ld.eld` |
| `eld-toolchain.cmake`, `eld-project-include.cmake`, `build-eld.sh` | CMake | selects the shim in a way that survives `CMakeLists` overwriting link flags |
| `eld.mk` | Make | `LD`, `LDFLAGS += -B…`, exported `COMPILER_PATH`; included by entry-point makefiles |
| `eld-args.gn` | GN | pins `use_lld`/`use_mold` off; shim spliced into every `tool("link")` |
| `meson` link arguments | Meson | injected after `project()` |
| `configure LDFLAGS` + libtool patch | Autotools | so shared-library links also go through eld |
| `eldconv-report.{json,md}` | all | every decision with `file:line` |

## How flags are mapped

Rules live in `eldconv/mapping.py`, scoped by source linker:

- **keep**: same spelling in eld (`--gc-sections`, `-Map`, `-z now`, `--wrap`)
- **rename**: different spelling (`--Map=` → `-Map=`, `--relocatable` → `-r`, `--dT` → `--default-script=`, `--lto-options` → `--flto-options`)
- **drop (warning)**: no equivalent and harmless (`--fork`, `--perf`, `--color-diagnostics`, CET `-z ibt/shstk`)
- **unsupported (error)**: an effect is lost (`--icf=*`, `-z pack-relative-relocs`, `--compress-debug-sections`)
- anything unknown is kept and reported; `-z` keywords are decided per keyword

The decision is filtered against a bundled snapshot of what a recent ld.eld accepts (eld `main` of
13 Oct 2026, reporting itself as 24.0) on x86_64, aarch64, arm, riscv32, riscv64, i386 and hexagon.
If your eld is older or newer, use `--eld-surface auto` (probes your `--eld` once, caches the result)
or build a file with `eldconv surface`. When the ld.eld in use (`--eld`, else `PATH`) lacks options
the snapshot assumes in its `--help`, `convert` warns that the snapshot is newer than your eld.

## What `check` verifies

1. the build exits 0;
2. at least one real link went through the shim (catches "selection silently ignored");
3. every linked output is a valid ELF;
4. no flag was reported as `Unrecognized option` by eld;
5. no binary in the tree was linked by *another* linker (read from ELF `.comment`/build-id);
6. no executable has undefined data symbols without a relocation (it would link and then crash).
7. eld did not skip a version script (an eld built without `-DELD_ENABLE_SYMBOL_VERSIONING=ON`,
   the default, ignores named version nodes; `--allow-ignored-version-scripts` makes it a warning).

Configure-time probe links (CMake `try_compile`, autoconf `conftest`, Meson sanity checks, kbuild
`ld-option`) are recognised and not judged as outputs.

## Known limits

- **eld itself** (`main` of 13 Oct 2026, still versioned 24.0): no ICF, no RELR, no GCC slim-LTO
  plugin (eldconv forces fat LTO objects; Clang LTO works). Older eld builds also lack an i386
  backend; recent ones link `-m elf_i386`, but an x86 kernel's final `vmlinux` link still stops on
  an eld program-header error, so use arm64, arm or riscv kernels.
- **eld on x86_64 emits no copy relocations**: a dynamic executable that uses shared-library
  data (`stdout`, `environ`, C++ vtables) links and then segfaults. `check` fails such binaries on
  purpose; `--allow-unresolved-data` downgrades that to a warning so the conversion itself can still
  be verified. aarch64, arm, riscv64 and hexagon are not affected by this check.
- Options computed by a build at run time (inside `$(shell …)`, configure probes) cannot be seen
  statically. The shim removes the harmless ones (`-z cet-report=…`, `-z ibt`, …); `check` reports the rest.
- A response file the converter can find in the tree is converted; one generated during the build is
  passed through and reported as an error.
- Linker selection made *outside* the build files (CI scripts, container config) is reported, not changed.
- Windows (PE/COFF) and Mach-O options are left alone; eld links ELF.
- Bazel is not supported.

More: [docs/VALIDATION.md](docs/VALIDATION.md) (real projects and kernel, bugs found),
[docs/DESIGN.md](docs/DESIGN.md) (pipeline).

## Development

```sh
pip install -e '.[dev]'
python -m pytest -q                                   # ~1100 tests; e2e uses a stub ld.eld
ELDCONV_TEST_ELD=/path/to/ld.eld python -m pytest -q  # same tests against a real eld
python -m ruff check eldconv tests tools
```

`tools/harvest_source_options.py` regenerates `eldconv/data/source_options.json`;
`eldconv surface` regenerates `eldconv/data/eld-surface.json`.

CI (`.github/workflows/ci.yml`) runs lint and tests on Python 3.12 and 3.14 and builds the wheel.
