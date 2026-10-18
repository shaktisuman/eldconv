"""Rewrite linker-flag fragments inside arbitrary build-file text.

Everything not recognised as a linker option is left byte-identical.
"""
from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple

from .flags import (LinkArg, emit_linker_item, emit_raw, emit_wl, emit_xlinker,
                    parse_linker_item, parse_raw, parse_wl, parse_xlinker)
from .mapping import Action, Diag, convert_args, find_rule

# A make/cmake variable reference is one atomic piece of a token even though it
# contains parentheses: `--orphan-handling=$(CONFIG_X)` must not be cut at the "$".
_VARREF = r"\$(?:\([^()\n]*\)|\{[^{}\n]*\}|[A-Za-z_]\w*)"
_TOK = r"""(?:""" + _VARREF + r"""|[^\s"'();<>\[\]])+"""
WL_RE = re.compile(r"-Wl," + _TOK)
# `@file` is a linker response file, but Apple's dyld install-name placeholders
# (@loader_path, @executable_path, @rpath) share the syntax and are not files.
DYLD_PLACEHOLDERS = ("loader_path", "executable_path", "rpath")
# `$@` (make's target) and `$$@` are not response files: exclude a preceding "$".
RESPONSE_RE = re.compile(r"(?<![\w=$(\{])@(" + _TOK + r")")
XLINKER_RE = re.compile(r"(?:-Xlinker\s+" + _TOK + r"\s*)+")
LINKER_SHELL_RE = re.compile(r"LINKER:SHELL:[^\"'();]+")
LINKER_RE = re.compile(r"LINKER:(?!SHELL:)" + _TOK)
FUSE_RE = re.compile(r"(?<![\w-])-fuse-ld=" + _TOK)
LDPATH_RE = re.compile(r"(?<![\w-])--ld-path=" + _TOK)
# bare long option not preceded by ',' '=' or a word char (so -Wl,--x is skipped)
BARE_RE = re.compile(r"(?<![\w,=\-/.$)}>:])(--[a-zA-Z][\w-]*)(=" + _TOK + r")?")
# A bare `--flag` is only treated as a linker option on lines that clearly carry
# linker context; otherwise `python gen.py --stats` would be "converted" too.
LINK_CONTEXT_RE = re.compile(
    r"-Wl,|-Xlinker|-fuse-ld|--ld-path|LINK_FLAGS|LINKOPTS"
    # kbuild spells these in lower case: ldflags-y, ldlibs-y, KBUILD_LDFLAGS
    r"|(?i:ldflags|ld_flags|ldlibs|linker|link_options|link_libraries)"
    r"|\$[({]LD[)}]|\bld\.(?:bfd|lld|gold|mold)\b"
    r"|^\s*(?:export\s+|override\s+)?LD\s*[:?+]?=")
# `-B<dir>` that points the compiler driver at another linker's `ld`: mold's
# documented recipe for GCC < 12 is `-B/usr/libexec/mold`. GCC takes the first
# -B prefix that has an `ld`, so left in place it would beat eld's shim.
OTHER_LINKER_B_RE = re.compile(
    r"(?<![\w-])-B\s*((?:[^\s\"'(),;\]]*/)?(?:libexec/mold|mold|lld|gold|ld\.(?:lld|gold|bfd))/?)"
    r"(?=[\s\"',;)\]]|$)")
EMPTY_QUOTES_RE = re.compile(r'(?<=\s)""(?=\s|$|\))')

# Make functions that *match or edit text* rather than pass flags to a linker:
# in `$(filter -fuse-ld=% --ld-path=%,$(KBUILD_HOSTLDFLAGS))` the flags are
# patterns being tested, so rewriting them changes what the build decides.
TEXT_FUNC_RE = re.compile(r"\$\((filter|filter-out|findstring|patsubst|subst|word|wordlist)\b")

# kbuild probes a linker flag with `$(call ld-option,--flag)`. That argument is a
# flag handed to the linker, so it converts -- the comma before it would otherwise
# make BARE_RE skip it. `cc-option` and friends carry COMPILER flags and are never
# touched.
CALL_OPTION_RE = re.compile(r"(\$\(call\s+ld-option,\s*)([^),]*)")


def text_function_spans(line: str) -> List[Tuple[int, int]]:
    """Character spans of make text-manipulation function calls in `line`."""
    spans: List[Tuple[int, int]] = []
    for m in TEXT_FUNC_RE.finditer(line):
        depth = 0
        for i in range(m.start() + 1, len(line)):
            if line[i] == "(":
                depth += 1
            elif line[i] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), i + 1))
                    break
        else:
            spans.append((m.start(), len(line)))
    return spans


# A bare `--flag` on a line with linker context still belongs to whichever
# command it is an argument of. These commands never link.
_NON_LINK_COMMAND_RE = re.compile(
    r"^(?:\$[({]PYTHON\w*[)}]|python\S*|perl|ruby|node|bash|sh|\S+\.(?:py|sh|pl|rb)|echo|printf|git|"
    r"cp|mv|rm|ln|install|sed|awk|grep|tar|gzip|cat|test|\[|\$[({](?:INSTALL\w*|RM|CP|MV|SED|AWK)[)}])$")
_SEGMENT_SPLIT_RE = re.compile(r"(?:;|&&|\|\||\||\t|^)\s*")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+")


def _segment_command(text: str, pos: int) -> str:
    """The command word of the shell segment containing `pos`, after any
    VAR=value prefixes: for `CC="x" LDFLAGS="y" $(PYTHON) f.py --a` it is $(PYTHON)."""
    starts = [m.end() for m in _SEGMENT_SPLIT_RE.finditer(text[:pos])]
    seg = text[(starts[-1] if starts else 0):pos].lstrip("@-+ ")
    while True:
        m = _ENV_ASSIGN_RE.match(seg)
        if not m:
            break
        seg = seg[m.end():]
    return seg.split(None, 1)[0] if seg.split() else ""


class Rewriter:
    """Stateful text rewriter for one source linker."""

    def __init__(self, linker: str, bare_requires_context: bool = True,
                 strip_empty_quotes: bool = False, shell: bool = False):
        self.linker = linker
        # Shell-ish input (configure.ac, Makefile.am recipes): a flag that is a
        # case pattern (`-fuse-ld=*)`, `-fuse-ld=lld|-fuse-ld=gold)`) is a test,
        # not a use, and removing it would break the script.
        self.shell = shell
        self.bare_requires_context = bare_requires_context
        # CMake: an argument emptied by conversion must go entirely, or
        # `set(X "" "-Wl,..")` turns the value into a list and leaks a ";"
        # into the link line.  In JSON (CMakePresets) "" is a valid value.
        self.strip_empty_quotes = strip_empty_quotes
        self.diags: List[Diag] = []
        self.response_files: set = set()
        # (path, is_linker_rsp, file, line, diag) for every @file reference
        self.response_refs: List[Tuple[str, bool, Optional[str], Optional[int], Diag]] = []
        self._protected: List[Tuple[int, int]] = []
        self._text = ""

    # -- helpers -------------------------------------------------------------
    def _note(self, diags: List[Diag], file: Optional[str], line: Optional[int]) -> None:
        for d in diags:
            d.file, d.line = file, line
        self.diags.extend(diags)

    def _wl(self, m: re.Match, file, line) -> str:
        args, d = convert_args(parse_wl(m.group(0)), self.linker)
        self._note(d, file, line)
        return emit_wl(args)

    def _xlinker(self, m: re.Match, file, line) -> str:
        vals = re.findall(r"-Xlinker\s+(" + _TOK + ")", m.group(0))
        args, d = convert_args(parse_xlinker(vals), self.linker)
        self._note(d, file, line)
        out = emit_xlinker(args)
        trailing = m.group(0)[len(m.group(0).rstrip()):]
        return (out + trailing) if out else ""

    def _linker_item(self, m: re.Match, shell: bool, file, line) -> str:
        body = m.group(0)[len("LINKER:"):]
        args, d = convert_args(parse_linker_item(body), self.linker)
        self._note(d, file, line)
        return emit_linker_item(args, shell)

    def _sel(self, m: re.Match, file, line) -> str:
        tok = m.group(0)
        name, val = tok.split("=", 1)
        args, d = convert_args([LinkArg(name, val, "=")], self.linker)
        self._note(d, file, line)
        return " ".join(emit_raw(args))

    def _bare(self, m: re.Match, file, line) -> str:
        if _NON_LINK_COMMAND_RE.match(_segment_command(self._text, m.start())):
            return m.group(0)   # `$(PYTHON) fuzz.py --debug=1`: someone else's option
        name = m.group(1)
        val = m.group(2)[1:] if m.group(2) else None
        arg = LinkArg(name, val, "=" if val is not None else "")
        rule = find_rule(arg, self.linker)
        if rule is None or rule.action is Action.KEEP:
            return m.group(0)
        args, d = convert_args([arg], self.linker)
        self._note(d, file, line)
        return " ".join(emit_raw(args))

    def _other_b(self, m: re.Match, file, line) -> str:
        self.diags.append(Diag("warn", m.group(0).strip(),
                               "removed: this -B points the compiler at another linker's `ld`, "
                               "which would take precedence over eld's shim", file, line))
        return ""

    def _call_option(self, m: re.Match, file, line) -> str:
        head, body = m.group(1), m.group(2)
        if not body.strip().startswith("-"):
            return m.group(0)
        args, d = convert_args(parse_raw(body.split()), self.linker)
        new = " ".join(emit_raw(args))
        if new == body.strip():
            return m.group(0)  # nothing changed: keep the original spacing
        self._note(d, file, line)
        return head + new

    def _guarded(self, fn, text: str):
        """Wrap a substitution callback so it skips make text-function arguments
        and, in shell input, case patterns."""
        def inner(m: re.Match) -> str:
            for lo, hi in self._protected:
                if lo <= m.start() < hi:
                    return m.group(0)
            if self.shell:
                nxt = text[m.end():m.end() + 1]
                if any(c in m.group(0) for c in "*?[") or nxt in (")", "|"):
                    return m.group(0)
                if text[:m.start()].rstrip().endswith(("|", "(")) and ")" in text[m.end():]:
                    return m.group(0)
            return fn(m)
        return inner

    def _sub(self, rx: re.Pattern, fn, text: str) -> str:
        self._protected = text_function_spans(text)
        self._text = text
        return rx.sub(self._guarded(fn, text), text)

    # -- public --------------------------------------------------------------
    def rewrite_line(self, text: str, file: Optional[str] = None, line: Optional[int] = None) -> str:
        if "@" in text and ("-Wl," in text or "-Xlinker" in text or LINK_CONTEXT_RE.search(text)):
            for m in RESPONSE_RE.finditer(text):
                if m.group(1).split("/")[0] in DYLD_PLACEHOLDERS:
                    continue
                if not text[:m.start()].strip(" \t-+@"):
                    continue    # make's `@` (silent) recipe prefix, as in `@echo LDFLAGS=...`
                if "@" in m.group(1):
                    continue    # an autoconf placeholder (`@VERSION@`, `@srcdir@/x.map`), not a file
                if text[:m.start()].rstrip().endswith(";"):
                    continue    # `all: ; @echo ...`: make's silent prefix after a ';'
                self.response_files.add(m.group(1))
                # `-Wl,@f` / `-Xlinker @f` hand the file to the linker; a bare
                # `@f` on a compiler line is a *driver* response file.
                head = text[:m.start()]
                tok_start = max(head.rfind(" "), head.rfind("\t"), head.rfind('"'), head.rfind("'")) + 1
                is_linker = (text[tok_start:m.start()].startswith("-Wl,")
                             or head.rstrip().endswith("-Xlinker"))
                d = Diag("error", "@" + m.group(1),
                         "response file: its contents are not visible to the converter "
                         "and are passed on unchanged; convert it by hand "
                         "(`eldconv flags " + ("--raw " if is_linker else "")
                         + "\"$(cat <file>)\"`)", file, line,
                         kind="rsp-linker" if is_linker else "rsp-driver")
                self.diags.append(d)
                self.response_refs.append((m.group(1), is_linker, file, line, d))
        if "-" not in text and "LINKER:" not in text:
            return text
        orig = text
        text = self._sub(OTHER_LINKER_B_RE, lambda m: self._other_b(m, file, line), text)
        text = self._sub(WL_RE, lambda m: self._wl(m, file, line), text)
        text = self._sub(LINKER_SHELL_RE, lambda m: self._linker_item(m, True, file, line), text)
        text = self._sub(LINKER_RE, lambda m: self._linker_item(m, False, file, line), text)
        text = self._sub(XLINKER_RE, lambda m: self._xlinker(m, file, line), text)
        text = self._sub(CALL_OPTION_RE, lambda m: self._call_option(m, file, line), text)
        text = self._sub(FUSE_RE, lambda m: self._sel(m, file, line), text)
        text = self._sub(LDPATH_RE, lambda m: self._sel(m, file, line), text)
        if not self.bare_requires_context or LINK_CONTEXT_RE.search(orig):
            text = self._sub(BARE_RE, lambda m: self._bare(m, file, line), text)
        if text != orig:
            if self.strip_empty_quotes:
                text = EMPTY_QUOTES_RE.sub("", text)
                if text.strip() == '""':
                    text = ""
                # CMake values are ";"-separated lists: an emptied element must
                # take its separator with it, or it becomes an empty list entry.
                text = re.sub(r'"\s*;+', '"', text)
                text = re.sub(r';+\s*"', '"', text)
                text = re.sub(r";{2,}", ";", text)
            lead = orig[: len(orig) - len(orig.lstrip())]
            body = text[len(lead):] if text.startswith(lead) else text.lstrip()
            body = re.sub(r"[ \t]{2,}", " ", body).strip()
            if self.strip_empty_quotes:  # CMake call arguments
                body = re.sub(r"\s+\)", ")", body)
            text = lead + body
            if orig.endswith("\r") and not text.endswith("\r"):
                text += "\r"  # keep CRLF files consistent
        return text

    def rewrite_text(self, text: str, file: Optional[str] = None) -> str:
        out = []
        for i, ln in enumerate(text.split("\n"), 1):
            out.append(self.rewrite_line(ln, file, i))
        return "\n".join(out)


def convert_driver_string(s: str, linker: str) -> Tuple[str, List[Diag]]:
    """Convert a compiler-driver flag string (e.g. an LDFLAGS value)."""
    rw = Rewriter(linker, bare_requires_context=False)
    return rw.rewrite_line(s), rw.diags


def convert_raw_argv(argv: List[str], linker: str) -> Tuple[List[str], List[Diag]]:
    """Convert a raw ld argv (direct linker invocation)."""
    args, d = convert_args(parse_raw(argv), linker)
    return emit_raw(args), d


def convert_raw_string(s: str, linker: str) -> Tuple[str, List[Diag]]:
    argv, d = convert_raw_argv(shlex.split(s), linker)
    return " ".join(shlex.quote(a) if re.search(r"\s", a) else a for a in argv), d
