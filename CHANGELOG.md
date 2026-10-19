# Changelog

## Unreleased

- Requires Python 3.12 or newer (was 3.8).

## 1.1.0

Tracks eld `main` as of 13 October 2026.

- New eld options mapped: `--pic-veneer` (kept; error on an eld without it), `--undefined-version`,
  `--no-undefined-version`, `--merge-exidx-entries`, `--no-merge-exidx-entries`; generic `-O<n>` is
  now accepted by eld.
- Bundled snapshot regenerated: adds riscv32 and i386 targets (387 of 758 options, 24 of 97 `-z`
  keywords). Targets whose baseline link fails are no longer counted as accepting every option.
- `--eld-surface auto`: probe the given `--eld` once and cache the result by binary identity.
  `convert` warns when the bundled snapshot assumes options the given eld lacks.
- The i386 obstacle is informational when the active snapshot has an i386 backend. Kernel trees
  get one warning, pointed at `arch/x86/realmode/rm/Makefile`, that an x86 `vmlinux` still fails
  in eld.
- `check` reports version scripts that eld ignored (an eld built without
  `ELD_ENABLE_SYMBOL_VERSIONING`, the default, skips named version nodes).
- `audit` no longer reports PE/COFF passthrough rules or the intentional `-O` drop, and checks
  options eld only accepts for ARM/AArch64 on those targets.
- `--eld-surface auto` cache is keyed on what the eld prints (wrapper scripts), written atomically;
  a malformed snapshot file is an error, not a traceback.
- `check` validates link flags against `ld.eld -m <emulation> --help` for every emulation the links
  used, so ARM/AArch64-only options (`--pic-veneer`) are no longer reported as unknown; options in
  configure-time feature probes no longer reach the "flags not listed" warning.
- `check` on a build that failed before any real link says so, instead of "build was up to date?".
- `flags` takes `--eld` for `--eld-surface auto`.
- `check --allow-ignored-version-scripts` downgrades skipped version scripts to a warning.

## 1.0.0

Production-readiness release.

- **eld option surface**: the converter now knows what the target eld actually accepts
  (eld ignores unknown options with only a warning). A bundled snapshot for ld.eld 24.0 on
  x86_64/aarch64/arm/riscv64/hexagon is used by default; `eldconv surface` builds one from your
  own eld, `--eld-surface` selects it. Dropped options carry a severity: harmless (warning) or
  effect-lost (error); `--strict` fails the conversion on the latter.
- **Build systems**: Meson and Autotools added (configure.ac, Makefile.am/.in, libtool patch);
  `--build-system` forces detection; hand-written `./configure` + `Makefile.in` trees handled.
- **Linker selection**: `COMPILER_PATH` export in addition to `-B`, absolute shim path (recursive
  `make -C` builds), removal of other linkers' `-B` dirs, direct linker invocations, response
  files, CMake presets, outside-of-build-file selection warnings, i386 warning.
- **Shim**: strips GCC/LLVM plugin arguments, drops `-z` hints eld rejects (CET, origin), GCC LTO
  forced to fat objects.
- **Checker**: timeouts, `--pre-build`, wrong-linker detection via ELF `.comment`/build-id,
  configure-time and feature-probe links recognised, ignored-option detection.
- **Safety**: output is written atomically; `--force` refuses non-eldconv directories.
- Validated with a real eld on zstd, libuv, fmt, lz4, lua, zlib, jq, curl and an arm64 kernel.
- **Binary sanity**: `check` fails an executable whose undefined data symbols (`stdout`, `environ`,
  ...) got no relocation -- eld 24.0 on x86_64 emits no copy relocations, so such programs link
  and then crash. `--allow-unresolved-data` downgrades it to a warning.
- Fixes from an independent review: CMake+Ninja `-B...;-fno-use-linker-plugin` list bug,
  `check --build-dir` could delete unrelated directories (now only inside the tree or a real
  CMake/Meson build dir), `--relocatable` is renamed to `-r` instead of dropped, weak data
  symbols, parallel `make -j` shim log interleaving, autoconf `@srcdir@/x` and `; @echo` no longer
  mistaken for response files, `LD := ld` usable before `eld.mk`, malformed ELF files never
  crash the checker, quoted `--cmake-arg`, symlinked/space-containing destinations rejected,
  missing eld in `check` exits 2.
- Known limits (eld 24.0): no i386 emulation, no ICF, no RELR, no x86_64 copy relocations,
  no GCC slim-LTO plugin.
