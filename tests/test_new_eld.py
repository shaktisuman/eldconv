"""Options and behaviour added to eld after the first snapshot: --pic-veneer,
--[no-]undefined-version, --[no-]merge-exidx-entries, an i386 backend, and the
machinery that keeps a bundled snapshot honest for older/newer eld builds."""
import json
import subprocess
from pathlib import Path

import pytest

from eldconv import surface
from eldconv.cli import main
from eldconv.flags import parse_wl
from eldconv.mapping import convert_args
from eldconv.obstacles import scan


def conv(spec, linker="bfd"):
    return convert_args(parse_wl(spec), linker)


def surf(accepted, targets=("x86_64", "aarch64"), rejected=()):
    d = {"eld_version": "GNU ld compatible linker - eld 24.0", "targets": list(targets),
         "options": {**{n: list(targets) for n in accepted}, **{n: [] for n in rejected}}, "z": {}}
    return surface.Surface.from_dict(d)


# ------------------------------------------------------------ the new options

@pytest.mark.parametrize("opt", ["--pic-veneer", "--undefined-version", "--no-undefined-version"])
def test_new_options_are_kept_when_eld_has_them(opt):
    with surface.use_surface(surf([opt])):
        out, d = conv(f"-Wl,{opt}", "lld")
    assert [a.name for a in out] == [opt] and not [x for x in d if x.level != "info"]


@pytest.mark.parametrize("opt", ["--undefined-version", "--no-undefined-version"])
def test_undefined_version_is_dropped_with_a_warning_on_an_older_eld(opt):
    with surface.use_surface(surf([], rejected=[opt])):
        out, d = conv(f"-Wl,{opt}", "lld")
    assert out == [] and d and d[0].level in ("warn", "error")


def test_pic_veneer_on_an_older_eld_is_an_error_not_a_silent_drop():
    """An eld that ignores --pic-veneer links without the veneers: the effect is lost."""
    with surface.use_surface(surf([], rejected=["--pic-veneer"])):
        out, d = conv("-Wl,--pic-veneer", "bfd")
    assert out == [] and d[0].level == "error"


def test_merge_exidx_entries_is_an_lld_spelling_eld_now_shares():
    with surface.use_surface(surf(["--no-merge-exidx-entries"])):
        out, d = conv("-Wl,--no-merge-exidx-entries", "lld")
    assert [a.name for a in out] == ["--no-merge-exidx-entries"] and not d


def test_bundled_snapshot_knows_the_new_options(bundled_surface):
    for o in ("--pic-veneer", "--undefined-version", "--no-undefined-version", "--no-merge-constants",
              "--replay", "--merge-exidx-entries", "--no-merge-exidx-entries"):
        assert bundled_surface.accepts(o) is True, o
    assert {"i386", "riscv32"} <= set(bundled_surface.targets)


# ------------------------------------------------------------------- i386

def _i386_tree(tmp_path: Path) -> Path:
    (tmp_path / "Makefile").write_text("LDFLAGS += -m elf_i386\nall:\n\ttrue\n")
    return tmp_path


def test_i386_is_a_warning_without_an_i386_backend(tmp_path):
    root = _i386_tree(tmp_path)
    with surface.use_surface(surf([], targets=("x86_64",))):
        diags = scan(root)
    d = [x for x in diags if "32-bit x86" in x.message]
    assert d and d[0].level == "warn"


def test_i386_is_informational_when_eld_has_the_backend(tmp_path):
    root = _i386_tree(tmp_path)
    with surface.use_surface(surf([], targets=("x86_64", "i386"))):
        diags = scan(root)
    d = [x for x in diags if "32-bit x86" in x.message]
    assert d and d[0].level == "info" and "older eld" in d[0].message


# ------------------------------------------------- probing and snapshot upkeep

def test_a_target_eld_cannot_link_does_not_count_as_accepting_everything(tmp_path):
    """On an eld without an i386 backend every option would otherwise 'pass' there."""
    def runner(cmd, **kw):
        if "-c" in cmd:                                   # compiling probe objects
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x7fELF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "fake 1\n", "")
        # linking: i386 objects fail outright; everything else rejects --bogus
        objs = [a for a in cmd if a.endswith(".o")]
        if any("i386-" in o for o in objs):
            return subprocess.CompletedProcess(cmd, 1, "", "Fatal: Emulation failed for target : i386\n")
        bad = any(a.startswith("--bogus") for a in cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "Warning: Unrecognized option `--bogus'\n" if bad else "")

    s = surface.build("/fake/ld.eld", extra_options=["--bogus"], runner=runner, jobs=2)
    assert "i386" not in s.targets
    assert s.options["--bogus"] == []


def test_stale_snapshot_detects_an_older_eld():
    old = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "  --gc-sections\n  --undefined-version\n", "")
    assert surface.stale_snapshot("/x/ld.eld", runner=old) == ["--no-merge-constants", "--replay"]
    new = lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "--no-merge-constants --undefined-version --replay\n", "")
    assert surface.stale_snapshot("/x/ld.eld", runner=new) == []
    silent = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    assert surface.stale_snapshot("/x/ld.eld", runner=silent) == []     # cannot tell: say nothing


def test_surface_cache_is_keyed_on_the_exact_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    eld = tmp_path / "ld.eld"
    eld.write_text("a")
    f1 = surface.cache_file(str(eld))
    eld.write_text("longer content")
    assert surface.cache_file(str(eld)) != f1
    assert str(f1).startswith(str(tmp_path / "cache"))


def test_cached_or_build_probes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    eld = tmp_path / "ld.eld"
    eld.write_text("x")
    calls = []

    def fake_build(path, **kw):
        calls.append(path)
        return surf(["--gc-sections"])
    monkeypatch.setattr(surface, "build", fake_build)
    a = surface.cached_or_build(str(eld))
    b = surface.cached_or_build(str(eld))
    assert len(calls) == 1 and a.accepts("--gc-sections") and b.accepts("--gc-sections")


def test_cli_surface_auto_uses_the_probed_snapshot(tmp_path, monkeypatch, capsys):
    eld = tmp_path / "ld.eld"
    eld.write_text("#!/bin/sh\n")
    eld.chmod(0o755)
    seen = []
    monkeypatch.setattr(surface, "cached_or_build", lambda p, **kw: seen.append(p) or surf(["--gc-sections"]))
    monkeypatch.setattr(surface, "cache_file", lambda p: tmp_path / "nope.json")
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text("LDFLAGS += -Wl,--gc-sections\nall:\n\ttrue\n")
    rc = main(["convert", str(src), str(tmp_path / "d"), "--eld", str(eld), "--eld-surface", "auto", "-q"])
    assert rc == 0 and seen == [str(eld)]
    assert "probing" in capsys.readouterr().err


def test_cli_warns_when_the_bundled_snapshot_is_newer_than_the_eld(tmp_path, capsys):
    eld = tmp_path / "ld.eld"
    eld.write_text("#!/bin/sh\necho '  --gc-sections  collect'\n")
    eld.chmod(0o755)
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text("all:\n\ttrue\n")
    main(["convert", str(src), str(tmp_path / "d"), "--eld", str(eld), "-q"])
    err = capsys.readouterr().err
    assert "newer than this ld.eld" in err and "--eld-surface auto" in err
    assert json.loads((Path(__file__).parent.parent / "eldconv" / "data" / "eld-surface.json").read_text())


def test_ignored_version_scripts_are_reported():
    from eldconv.checker import ignored_version_scripts
    text = ("Warning: Only anonymous version nodes are supported. Ignoring version script "
            "`arch/x86/entry/vdso/vdso32/vdso32.lds'\nWarning: Only anonymous version nodes are "
            "supported. Ignoring version script `arch/x86/entry/vdso/vdso32/vdso32.lds'\nNote: other\n")
    assert ignored_version_scripts(text) == ["arch/x86/entry/vdso/vdso32/vdso32.lds"]
    assert ignored_version_scripts("Warning: fine") == []


def test_x86_kernel_tree_gets_the_vmlinux_warning_even_with_an_i386_backend(tmp_path):
    (tmp_path / "Kbuild").write_text("")
    (tmp_path / "arch" / "x86" / "realmode" / "rm").mkdir(parents=True)
    (tmp_path / "arch" / "x86" / "realmode" / "rm" / "Makefile").write_text("LDFLAGS_realmode.elf := -m elf_i386\n")
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n")
    with surface.use_surface(surf([], targets=("x86_64", "i386"))):
        diags = scan(tmp_path)
    d = [x for x in diags if "Linux kernel: if you build it for x86" in x.message]
    assert d and d[0].level == "warn" and "vmlinux" in d[0].message
    assert (d[0].file, d[0].line) == ("arch/x86/realmode/rm/Makefile", 1)


# --------------------------------------------------------- snapshot files and cache

@pytest.mark.parametrize("bad", ["[]", '{"options": 5}', '{"targets": "x86_64"}', "",
                                 '{"targets": [], "options": {}}', '{"targets": ["x86_64"], "options": {"-q": 1}}'])
def test_malformed_snapshot_is_a_value_error(tmp_path, bad):
    f = tmp_path / "s.json"
    f.write_text(bad)
    with pytest.raises(ValueError):
        surface.Surface.load(f)


def test_cli_rejects_a_malformed_snapshot_cleanly(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text("[]")
    src = tmp_path / "s"
    src.mkdir()
    (src / "Makefile").write_text("all:\n\ttrue\n")
    rc = main(["convert", str(src), str(tmp_path / "d"), "--eld-surface", str(f), "-q"])
    assert rc != 0 and "cannot read eld surface" in capsys.readouterr().err


def test_cache_key_follows_what_a_wrapper_runs(tmp_path, monkeypatch):
    """A wrapper script's own size/mtime do not change when the eld behind it is rebuilt."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    eld = tmp_path / "ld.eld"
    eld.write_text("#!/bin/sh\nexec /somewhere/ld.eld \"$@\"\n")
    help_text = ["--gc-sections\n"]
    runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, help_text[0], "")
    a = surface.cache_file(str(eld), runner=runner)
    help_text[0] = "--gc-sections --replay\n"
    assert surface.cache_file(str(eld), runner=runner) != a


def test_corrupt_cache_is_reprobed_and_rewritten_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    eld = tmp_path / "ld.eld"
    eld.write_text("x")
    f = surface.cache_file(str(eld))
    f.parent.mkdir(parents=True)
    f.write_text("[1, 2")                                   # truncated by a crashed run
    monkeypatch.setattr(surface, "build", lambda p, **kw: surf(["--gc-sections"]))
    s = surface.cached_or_build(str(eld))
    assert s.accepts("--gc-sections")
    assert surface.Surface.load(f).accepts("--gc-sections")
    assert [x.name for x in f.parent.iterdir()] == [f.name]   # no temp file left behind


# ------------------------------------------------------------------ audit

def test_audit_skips_passthrough_and_intentional_drops_and_checks_other_targets(tmp_path):
    from eldconv.audit import audit

    def runner(cmd, **kw):
        if "-c" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x7fELF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, "  -O  --gc-sections\n", "")
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "eld 24.0\n", "")
        objs = " ".join(a for a in cmd if a.endswith(".o"))
        arm = "aarch64-" in objs or "arm-" in objs
        opt = cmd[1]
        if opt.startswith("--pic-veneer") and arm:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if opt.startswith("-") and opt not in ("-o", "-O", "--gc-sections") and opt != "-z":
            return subprocess.CompletedProcess(cmd, 0, "", f"Warning: Unrecognized option `{opt}'\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = audit("/fake/ld.eld", runner=runner)
    by = {f.option: f for f in res.findings}
    assert "--out-implib" not in by                     # PE/COFF passthrough
    assert "-O" not in by                               # dropped on purpose
    assert by["--pic-veneer"].kind == "confirmed" and "aarch64" in by["--pic-veneer"].note


# ------------------------------------------------------- check: per-target help, probes

def _checker_env(tmp_path):
    from tests.test_checker import FIX, make_runner
    from eldconv.generator import convert_project
    eld = tmp_path / "ld.eld"
    eld.write_text("#!/bin/sh\nexit 0\n")
    eld.chmod(0o755)
    dst = tmp_path / "c"
    convert_project(FIX / "make_gold", dst, eld=str(eld))
    return dst, make_runner


def test_emulations_are_collected_in_both_spellings():
    from eldconv.checker import LinkInvocation, emulations
    invs = [LinkInvocation("/", ["-maarch64elf", "-o", "a"]), LinkInvocation("/", ["-m", "armelf", "-mllvm", "x"]),
            LinkInvocation("/", ["-m", "aarch64elf"])]
    assert emulations(invs) == ["aarch64elf", "armelf"]


def test_target_only_options_are_not_reported_as_unknown(tmp_path):
    """--pic-veneer is only in `ld.eld -m aarch64elf --help`, not in the host driver's --help."""
    from eldconv.checker import run_check
    dst, make_runner = _checker_env(tmp_path)
    base = make_runner(link_outputs={"vmlinux": ["-maarch64elf", "--gc-sections", "--pic-veneer", "--fork", "a.o"]})
    seen = []

    def runner(cmd, **kw):
        if cmd[0].endswith("ld.eld") and cmd[1:3] == ["-m", "aarch64elf"]:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "  --pic-veneer   Force PIC veneers\n", "")
        return base(cmd, **kw)
    res = run_check(dst, runner=runner)
    assert seen and res.unknown_flags == ["--fork"], res.unknown_flags


def test_feature_probe_options_do_not_reach_the_unknown_flags_warning(tmp_path):
    """kbuild's ld-option offers --pack-dyn-relocs=relr to see whether the linker has it."""
    from eldconv.checker import run_check
    dst, make_runner = _checker_env(tmp_path)
    runner = make_runner(link_outputs={"app": ["--gc-sections", "main.o"],
                                       "conftest": ["-shared", "--pack-dyn-relocs=relr", "conftest.o"]})
    res = run_check(dst, runner=runner)
    assert "--pack-dyn-relocs" not in res.unknown_flags
    assert not any("--pack-dyn-relocs" in w for w in res.warnings)


def test_failed_build_without_links_says_so_instead_of_up_to_date(tmp_path):
    from eldconv.checker import run_check
    dst, make_runner = _checker_env(tmp_path)
    res = run_check(dst, runner=make_runner(build_rc=1, write_log=False))
    assert any("failed before any real link" in p for p in res.problems)
    assert not any("up to date" in p or "shim log empty" in p for p in res.problems)


def test_ignored_version_scripts_fail_check_unless_allowed(tmp_path):
    from eldconv.checker import run_check
    dst, make_runner = _checker_env(tmp_path)
    base = make_runner(link_outputs={"app": ["--gc-sections", "main.o"]})
    warn = ("Warning: Only anonymous version nodes are supported. Ignoring version script `lib.map'\n")

    def runner(cmd, **kw):
        r = base(cmd, **kw)
        if cmd[0] == "make" and "clean" not in cmd:
            return subprocess.CompletedProcess(cmd, r.returncode, r.stdout, (r.stderr or "") + warn)
        return r
    res = run_check(dst, runner=runner)
    assert not res.ok and any("lib.map" in p for p in res.problems)
    res = run_check(dst, runner=runner, allow_ignored_version_scripts=True)
    assert res.ok and any("lib.map" in w for w in res.warnings)
    assert main(["check", str(dst), "--allow-ignored-version-scripts", "--dry-run"]) == 0
