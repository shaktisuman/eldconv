"""Make and shell lines continued with a backslash."""
from eldconv.converters.autotools import rewrite_configure_ac
from eldconv.converters.cmake import rewrite_cmake_text
from eldconv.converters.make import rewrite_make_text


def _errors(diags):
    return [(d.line, d.flag) for d in diags if d.level == "error"]


def test_continued_ldflags_keep_their_linker_context():
    new, d = rewrite_make_text("LDFLAGS = -O2 \\\n\t--icf=all \\\n\t--gc-sections\n", "lld",
                               add_include=False)
    assert new == "LDFLAGS = -O2 \\\n\t\\\n\t--gc-sections\n"
    assert _errors(d) == [(2, "--icf=all")]


def test_context_ends_with_the_logical_line():
    text = "LDFLAGS = -O2 \\\n\t-lm\nTOOLFLAGS = --icf=all\nX = a \\\n\t--icf=all\n"
    new, d = rewrite_make_text(text, "lld", add_include=False)
    assert new == text and not d


def test_xlinker_value_on_the_next_line_is_converted():
    text = "LDFLAGS += -Xlinker \\\n\t--Map=x.map \\\n\t-Xlinker \\\n\t--icf=all\nall:\n"
    new, d = rewrite_make_text(text, "lld", add_include=False)
    assert new == "LDFLAGS += -Xlinker \\\n\t-Map=x.map \\\n\t\\\n\t\nall:\n"
    assert _errors(d) == [(4, "--icf=all")]
    assert any(x.flag == "--Map=x.map" and "renamed" in x.message for x in d)


def test_xlinker_pair_split_over_lines_is_left_alone():
    text = "LDFLAGS += -Xlinker \\\n\t-z \\\n\t-Xlinker \\\n\tnow\n"
    new, d = rewrite_make_text(text, "lld", add_include=False)
    assert new == text


def test_configure_ac_continuations():
    text = 'LDFLAGS="$LDFLAGS \\\n  --icf=all"\n'
    new, d = rewrite_configure_ac(text, "lld")
    assert "--icf" not in new and _errors(d) == [(2, "--icf=all")]


def test_cmake_does_not_treat_backslash_as_continuation():
    text = 'set(LINKER_FLAGS "-O2 \\\\")\nset(OTHER "--icf=all")\n'
    new, d = rewrite_cmake_text(text, "lld")
    assert new == text
