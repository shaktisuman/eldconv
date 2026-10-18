"""Randomized property tests.

These generate flag strings from a grammar and assert invariants that must hold
for *every* input, rather than checking hand-picked examples. Seeded, so a
failure is reproducible; the seed is printed in the assertion message.
"""
import random
import re
import shlex

import pytest

from eldconv import LINKERS
from eldconv.flags import (emit_wl, parse_wl)
from eldconv.mapping import Action, convert_args, find_rule
from eldconv.rewrite import Rewriter, convert_driver_string, convert_raw_argv

SEEDS = list(range(60))

# A grammar of realistic linker option shapes.
NO_VALUE = ["--gc-sections", "--as-needed", "--no-as-needed", "--whole-archive",
            "--no-whole-archive", "--start-group", "--end-group", "--eh-frame-hdr",
            "--export-dynamic", "-Bsymbolic", "--strip-all", "--emit-relocs",
            "--fatal-warnings", "--no-undefined", "--build-id", "--relax", "-shared",
            "-pie", "--no-pie", "-static", "--demangle", "--cref", "--print-map"]
EQ_VALUE = [("--hash-style", "gnu"), ("--build-id", "sha1"), ("--sort-section", "name"),
            ("--icf", "all"), ("--compress-debug-sections", "zlib"), ("--wrap", "malloc"),
            ("--defsym", "foo=1"), ("--unresolved-symbols", "ignore-all"),
            ("--image-base", "0x10000"), ("--thinlto-jobs", "4"), ("-Ttext", "0x8000")]
SEP_VALUE = [("-Map", "out.map"), ("-o", "a.out"), ("-T", "link.ld"), ("-e", "_start"),
             ("--wrap", "free"), ("-rpath", "$ORIGIN/lib"), ("-soname", "libx.so.1"),
             ("--version-script", "v.map"), ("--exclude-libs", "ALL"),
             ("--dynamic-linker", "/lib/ld.so")]
ATTACHED = ["-lm", "-lpthread", "-L/usr/lib", "-L.", "-O2", "-O1"]
Z_KEYS = ["now", "lazy", "relro", "norelro", "noexecstack", "defs", "muldefs", "text",
          "pack-relative-relocs", "ibt", "shstk", "max-page-size=4096", "separate-code",
          "some-future-keyword"]
PER_LINKER = {
    "lld": ["--icf=all", "--lto-O3", "--time-trace", "--color-diagnostics",
            "--warn-backrefs", "--pack-dyn-relocs=relr", "--symbol-ordering-file=o.txt"],
    "gold": ["--icf=safe", "--incremental", "--detect-odr-violations", "--stats",
             "--section-ordering-file=o.txt", "--icf-iterations=3"],
    "mold": ["--fork", "--no-fork", "--perf", "--quick-exit", "--repro",
             "--spare-dynamic-tags=5", "--separate-debug-file"],
    "bfd": ["--no-warn-rwx-segments", "--print-memory-usage", "--ctf-variables",
            "--warn-execstack", "--enable-non-contiguous-regions"],
}
POSITIONAL = ["main.o", "crt1.o", "libfoo.a", "sub/dir/bar.o"]


def gen_tokens(rng, linker, n):
    """Generate n flat linker tokens (as they'd appear inside -Wl,)."""
    out = []
    for _ in range(n):
        kind = rng.choice(["none", "eq", "sep", "att", "z", "specific", "pos"])
        if kind == "none":
            out.append(rng.choice(NO_VALUE))
        elif kind == "eq":
            k, v = rng.choice(EQ_VALUE)
            out.append(f"{k}={v}")
        elif kind == "sep":
            k, v = rng.choice(SEP_VALUE)
            out.extend([k, v])
        elif kind == "att":
            out.append(rng.choice(ATTACHED))
        elif kind == "z":
            z = rng.choice(Z_KEYS)
            out.extend(["-z", z] if rng.random() < 0.5 else [f"-z{z}"])
        elif kind == "specific":
            out.append(rng.choice(PER_LINKER[linker]))
        else:
            out.append(rng.choice(POSITIONAL))
    return out


# --------------------------------------------------------------------------- invariants

@pytest.mark.parametrize("seed", SEEDS)
def test_parse_emit_roundtrip_is_lossless(seed):
    """parse(emit(parse(x))) == parse(x): tokenising never loses or invents tokens."""
    rng = random.Random(seed)
    for linker in LINKERS:
        toks = gen_tokens(rng, linker, rng.randint(1, 12))
        src = "-Wl," + ",".join(toks)
        once = parse_wl(src)
        twice = parse_wl(emit_wl(once))
        assert [a.parts() for a in once] == [a.parts() for a in twice], (seed, src)
        # and the flat token stream is preserved exactly
        flat = []
        for a in once:
            flat.extend(a.parts())
        assert ",".join(flat) == ",".join(toks).replace("-z,", "-z,"), (seed, src)


@pytest.mark.parametrize("seed", SEEDS)
def test_conversion_is_idempotent(seed):
    """Converting an already-converted string must be a no-op."""
    rng = random.Random(seed)
    for linker in LINKERS:
        src = "-Wl," + ",".join(gen_tokens(rng, linker, rng.randint(1, 12)))
        once, _ = convert_driver_string(src, linker)
        twice, diags = convert_driver_string(once, linker)
        assert once == twice, (seed, linker, src, once, twice)


@pytest.mark.parametrize("seed", SEEDS)
def test_positional_inputs_are_never_lost_or_reordered(seed):
    """Object files and archives must survive conversion untouched and in order."""
    rng = random.Random(seed)
    for linker in LINKERS:
        toks = gen_tokens(rng, linker, rng.randint(2, 14))
        args = parse_wl("-Wl," + ",".join(toks))
        before = [a.name for a in args if a.positional]
        out, _ = convert_args(args, linker)
        after = [a.name for a in out if a.positional]
        assert before == after, (seed, linker, toks)


@pytest.mark.parametrize("seed", SEEDS)
def test_no_orphan_values_after_conversion(seed):
    """Dropping an option must drop its value too, never leave it as a bare token.

    Any positional in the output must have been a positional in the input.
    """
    rng = random.Random(seed)
    for linker in LINKERS:
        toks = gen_tokens(rng, linker, rng.randint(2, 14))
        args = parse_wl("-Wl," + ",".join(toks))
        allowed = {a.name for a in args if a.positional}
        out, _ = convert_args(args, linker)
        for a in out:
            if a.positional:
                assert a.name in allowed, (seed, linker, toks, a.name)


@pytest.mark.parametrize("seed", SEEDS)
def test_kept_options_keep_their_value_and_spelling(seed):
    """A KEEP rule must not alter the option at all."""
    rng = random.Random(seed)
    for linker in LINKERS:
        for a in parse_wl("-Wl," + ",".join(gen_tokens(rng, linker, 10))):
            rule = find_rule(a, linker)
            if rule is None or rule.action is not Action.KEEP:
                continue
            out, _ = convert_args([a], linker)
            assert [x.parts() for x in out] == [a.parts()], (seed, linker, a.parts())


@pytest.mark.parametrize("seed", SEEDS)
def test_output_is_shell_parseable_and_has_no_empty_tokens(seed):
    rng = random.Random(seed)
    for linker in LINKERS:
        src = "-Wl," + ",".join(gen_tokens(rng, linker, rng.randint(1, 12)))
        out, _ = convert_driver_string(src, linker)
        toks = shlex.split(out)
        assert all(t for t in toks), (seed, out)
        for t in toks:
            if t.startswith("-Wl,"):
                assert ",," not in t and not t.endswith(","), (seed, out)
        assert "  " not in out and out == out.strip(), (seed, repr(out))


@pytest.mark.parametrize("seed", SEEDS)
def test_every_dropped_flag_is_reported(seed):
    """Anything removed from the command line must produce a diagnostic."""
    rng = random.Random(seed)
    for linker in LINKERS:
        toks = gen_tokens(rng, linker, rng.randint(1, 12))
        args = parse_wl("-Wl," + ",".join(toks))
        out, diags = convert_args(args, linker)
        removed = len(args) - len(out)
        reported = sum(1 for d in diags if d.level in ("warn", "error"))
        assert reported >= removed, (seed, linker, toks, [d.flag for d in diags])


@pytest.mark.parametrize("seed", SEEDS)
def test_wl_xlinker_and_raw_agree(seed):
    """The same options must convert identically however they are spelled."""
    rng = random.Random(seed)
    for linker in LINKERS:
        toks = gen_tokens(rng, linker, rng.randint(1, 10))
        via_wl, _ = convert_driver_string("-Wl," + ",".join(toks), linker)
        wl_tokens = []
        for t in via_wl.split():
            wl_tokens.extend(t[4:].split(",") if t.startswith("-Wl,") else [t])
        via_x, _ = convert_driver_string(" ".join(f"-Xlinker {t}" for t in toks), linker)
        x_tokens = re.findall(r"-Xlinker (\S+)", via_x)
        via_raw, _ = convert_raw_argv(toks, linker)
        assert wl_tokens == x_tokens == via_raw, (seed, linker, toks)


@pytest.mark.parametrize("seed", SEEDS)
def test_linker_scoped_rules_do_not_leak(seed):
    """A rule scoped to one linker must not fire for another."""
    rng = random.Random(seed)
    for linker in LINKERS:
        for other in LINKERS:
            if other == linker:
                continue
            for tok in PER_LINKER[linker]:
                a = parse_wl("-Wl," + tok)[0]
                rule = find_rule(a, linker)
                other_rule = find_rule(a, other)
                if rule is None or other_rule is None:
                    continue
                if rule.linkers != other_rule.linkers:
                    assert linker in rule.linkers, (tok, linker)


@pytest.mark.parametrize("seed", SEEDS)
def test_rewriting_a_makefile_never_corrupts_unrelated_text(seed):
    """Lines with no linker content must come back byte-identical."""
    rng = random.Random(seed)
    noise = ["CC := gcc", "\t@echo building $@", "obj-y += foo.o", "# comment --with-x",
             "SRCS = $(wildcard *.c)", "\tpython3 gen.py --stats --perf > $@",
             "ifeq ($(CONFIG_X),y)", "endif", "VPATH = src:include",
             "\t$(CC) -c -o $@ $< -DFOO=1", "help:", "\t@true"]
    for linker in LINKERS:
        rw = Rewriter(linker)
        for line in noise:
            assert rw.rewrite_line(line) == line, (seed, linker, line)
        assert rw.diags == [], [d.flag for d in rw.diags]


@pytest.mark.parametrize("seed", SEEDS)
def test_unknown_options_are_always_preserved(seed):
    """An option the table does not know must never be silently dropped."""
    rng = random.Random(seed)
    for linker in LINKERS:
        name = "--zz-" + "".join(rng.choice("abcdefgh") for _ in range(6))
        tok = name + (f"={rng.randint(0, 99)}" if rng.random() < 0.5 else "")
        out, diags = convert_driver_string(f"-Wl,--gc-sections,{tok}", linker)
        assert tok in out, (seed, linker, tok, out)
        assert any(d.level == "info" for d in diags)
