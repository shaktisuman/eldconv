# eldconv — design

Goal: take a project that links with bfd, lld, gold or mold (GNU or LLVM toolchain) and builds
with CMake, Make, GN, Ninja, Meson or Autotools, emit a converted copy that links with eld, and
prove that eld did the links.

## Pipeline

```
detect  ->  convert (flags + build files)  ->  generate (new dir)  ->  check (build + verify)
```

1. **detect** (`eldconv/detect.py`)
   Scan the build files (CMakeLists.txt, *.cmake, CMakeCache.txt, CMakePresets.json, Makefile,
   *.mk, *.gn/*.gni, build.ninja, meson.build, configure.ac, Makefile.am/.in) for `-fuse-ld=`,
   `--ld-path=`, `CMAKE_LINKER[_TYPE]`, `LD=…`, `use_lld`, `mold -run` and compiler ids.
   Output: build system, toolchain (gnu/llvm), source linker, conditional linkers, evidence.

2. **flags** (`eldconv/flags.py`)
   Tokenise linker options out of driver strings: `-Wl,a,b`, `-Xlinker a`, CMake `LINKER:` /
   `LINKER:SHELL:`, and raw `ld` command lines. Values are grouped (`-Map,file`, `--icf=all`,
   `-znow`, `-O2`) into `LinkArg` records and re-emitted in the same form.

3. **mapping** (`eldconv/mapping.py`) and **surface** (`eldconv/surface.py`)
   A rule table scoped per source linker: `KEEP`, `RENAME`, `DROP` (no equivalent, harmless,
   `warn`) and `UNSUPPORTED` (an effect is lost, `error`). The decision is then filtered against
   the *surface*, an empirical snapshot of which options and `-z` keywords a real ld.eld accepts
   per target. eld ignores unknown options with only a warning, so an option eld lacks is dropped
   with a severity even when the table would keep it. Unknown flags are kept and reported.
   The target is "any" unless `--target` names it (a context variable, like the surface), or the
   options come with their own `-m EMULATION`.
   The bundled snapshot is from eld `main` of 13 Oct 2026; `--eld-surface auto` probes and caches
   the user's own eld; `audit` compares the table with a real eld.

4. **rewrite** (`eldconv/rewrite.py`, `eldconv/converters/{cmake,make,gn,meson,autotools}.py`)
   Text-level rewriting of build files: replace every linker-flag fragment with its converted
   form; strip `-fuse-ld` / `--ld-path` / `CMAKE_LINKER_TYPE` / `LD=ld.xxx` / `mold -run`; leave
   everything else byte-identical. `gn.py` also handles standalone `build.ninja`.

5. **obstacles** (`eldconv/obstacles.py`)
   Things that cannot be converted and are only reported: linker selection outside the build files
   (CI scripts, container config), response files, kbuild's linker-identity branches, i386 links.

6. **generate** (`eldconv/generator.py`)
   Copy the tree to a temporary sibling of the new dir, apply rewrites, add the files below, then
   rename into place (atomic):
   - `eld-bin/ld` shim: logs every invocation (when `ELDCONV_LINK_LOG` is set), strips GCC's LTO
     plugin arguments and a few `-z` hints eld rejects, and execs `ld.eld`. Selected with
     `-B<eld-bin>/` (gcc and clang), plus `COMPILER_PATH` and `LD=` where the build system uses them.
   - CMake: `eld-toolchain.cmake`, `eld-project-include.cmake` (`add_link_options`) and `build-eld.sh`.
   - Make: `eld.mk`, included by the entry-point makefiles.
   - GN: `eld-args.gn`; Meson: link arguments after `project()`; Autotools: `configure` LDFLAGS and
     a libtool patch.
   - `eldconv-report.json` / `.md` with every decision and its `file:line`.

7. **check** (`eldconv/checker.py`)
   Configure and build the converted tree from clean through the shim, parse the shim log, and
   assert: the build succeeded; at least one real link went through eld (configure probes and
   `try_compile` links are recognised and not judged); every output is a valid ELF; eld reported
   no ignored option; no binary in the tree was linked by another linker (ELF `.comment`,
   build-id); no executable has undefined data symbols without a relocation; eld skipped no
   version script. Writes `eldconv-check.json` into the build directory.

## LTO

GCC passes `-plugin liblto_plugin.so` through collect2, and eld cannot load it. The shim strips the
plugin, GNU toolchains get `-fno-use-linker-plugin`, and objects are compiled with
`-ffat-lto-objects`, so GCC LTO builds link correctly but without link-time optimisation. Clang LTO
works through eld.

## Verbosity

`-v` explains each decision, `-vv` adds every file and flag. Diagnostics are streamed by the
library only at `-v` and above; at level 0 the CLI prints them once from the report, so nothing is
duplicated. All progress goes to stderr.

## Tests

`pytest tests/`: unit tests per module, randomised and corner-case tests, and end-to-end tests
that use a stub `ld.eld` (a wrapper over the system `ld`) so the whole pipeline runs without eld.
`ELDCONV_TEST_ELD=/path/to/ld.eld` runs the same tests against a real eld.

## Validated against

See [VALIDATION.md](VALIDATION.md). In short:

- test projects for every build system and source linker, built end to end where the tool is
  installed (the GN end-to-end test runs only when `gn` is on `PATH`)
- real eld on zstd, libuv, fmt, lz4, lua, zlib, jq and curl (Make, CMake, Autotools): every link
  goes through eld; x86_64 executables that use shared-library data fail `check` because eld emits
  no copy relocations (`--allow-unresolved-data` passes them)
- an arm64 `tinyconfig` kernel with real eld: converts, links and passes `check`
- an x86_64 defconfig kernel: converts; with a stub eld it links fully, with real eld it stops at
  `vmlinux` on an eld error
- Chromium's real `build/` tree (GN): detected and converted; a Chromium-shaped GN project built
  end to end with gn + ninja and linked with eld

## Known limits

- The rule table is best effort; the surface, `audit` and `check` are the source of truth.
- CMake files that fully overwrite `CMAKE_EXE_LINKER_FLAGS` are handled by the project-include
  hook, but exotic generators and expressions may need manual edits.
- Bazel is not covered.
- eld limits (no ICF, no RELR, no GCC slim LTO, no x86_64 copy relocations, no symbol versions
  unless eld is built with `ELD_ENABLE_SYMBOL_VERSIONING`, no i386 on builds before late
  September 2026) cannot be converted around.
