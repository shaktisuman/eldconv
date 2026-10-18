"""GN (.gn/.gni) and Ninja (build.ninja) rewriting.

GN is what Chromium and its dependents use. Flags live in string lists:

    ldflags += [ "-Wl,--icf=all", "-fuse-ld=lld" ]

so each list element is converted independently and elements that convert to
nothing are removed from the list. Linker selection (`-fuse-ld=`, `--ld-path=`,
`-B<lld dir>`) is replaced by the eld shim, and the `use_lld` / `use_mold` GN
args are pinned false so the rest of the build stops assuming that linker.

build.ninja is *generated* by `gn gen`, so converting GN source and re-running
gn is the supported path. For a tree that only ships a generated build.ninja,
the link rules are rewritten in place as a fallback.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from ..mapping import Diag
from ..rewrite import Rewriter

# One "quoted string" element of a GN list.
GN_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# `ldflags = [ ... ]` / `ldflags += [ ... ]`, possibly spanning lines.
GN_LDFLAGS_START_RE = re.compile(r"^\s*(?:\w+\.)?(ldflags|libs|lib_dirs)\s*(?:\+=|=)\s*\[")
# GN args that name a linker.
GN_USE_LINKER_RE = re.compile(r"^(\s*)(use_lld|use_mold|use_gold|use_bfd)(\s*=\s*)(\S+)")
GN_LINKER_ASSERT_RE = re.compile(r"^\s*assert\(\s*(use_lld|use_mold)\b")

# Ninja link rules: the command line of a rule whose name mentions linking.
NINJA_RULE_RE = re.compile(r"^rule\s+(\S+)")
NINJA_COMMAND_RE = re.compile(r"^(\s+command\s*=\s*)(.*)$")
NINJA_LDFLAGS_RE = re.compile(r"^(\s*(?:ldflags|LDFLAGS|link_flags|LINK_FLAGS)\s*=\s*)(.*)$")


def _convert_gn_string(rw: Rewriter, body: str, file: str, line: int) -> str:
    """Convert the contents of one GN quoted string.

    A string ending in "," or "=" is a *prefix* that GN concatenates a value
    onto ("--ld-path=" + rebase_path(...), "-Wl,-add_ast_path,"). The trailing
    separator carries meaning, so it is held back over the conversion and put
    back if the flag survives.
    """
    # GN strings carry $variables and {{ninja}} placeholders; the rewriter
    # already treats ${...} as atomic, and {{...}} has no flag syntax in it.
    sep = ""
    if body.endswith(("=", ",")) and len(body) > 1:
        sep, body = body[-1], body[:-1]
    # The value is supplied by the concatenation that follows, so stand one in
    # while converting; without it `--version-script=` looks like a flag whose
    # value was lost.
    placeholder = "ELDCONVVALUE" if sep else ""
    before = len(rw.diags)
    new = rw.rewrite_line(body + (sep + placeholder if sep else ""), file, line)
    if placeholder:
        # the stand-in is an implementation detail; keep it out of the report
        for d in rw.diags[before:]:
            d.flag = d.flag.replace(sep + placeholder, sep).replace(placeholder, "")
        if not new.endswith(sep + placeholder):
            return new.replace(sep + placeholder, "")
        new = new[: -len(placeholder)]
    return new


GN_TOOL_RE = re.compile(r'^\s*tool\(\s*"(\w+)"\s*\)')
# The command may be assembled in a helper variable first
# (`link_command = "$ld {{ldflags}} -o ..."`), which is the real link line.
GN_COMMAND_RE = re.compile(
    r'^(\s*(?:command|link_command|solink_command|rust_link_command)\s*=\s*")(\S+\s+)(.*)$')

# A driver we can hand `-B<dir>/` to. Anything else -- a python wrapper
# ("$python_path" ... -- $link_command), Apple's linker_driver, or MSVC's
# $link -- would either swallow the flag or choke on it, so it is left alone
# and the real command it wraps is spliced instead.
GN_DRIVER_RE = re.compile(
    r'^(?:\\?"?\$\{?(?:ld|cc|cxx|clang|clang_cc|clang_cxx)\}?\\?"?'
    r'|(?:.*/)?(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+))[\s"\\]*$')

# Toolchains for platforms eld does not target; splicing into them is noise at
# best and breaks the command at worst.
NON_ELF_TOOLCHAIN_RE = re.compile(r"(?:^|/)(?:win|apple|mac|ios|zos|nacl)(?:/|\.gni?$)")


def rewrite_gn_text(text: str, linker: str, file: str = "BUILD.gn",
                    select_flags: str = "") -> Tuple[str, List[Diag]]:
    """Convert one GN file.

    `select_flags` (e.g. `-B/path/eld-bin/ -fno-use-linker-plugin`) is spliced
    into the command of every `tool("link")` / `tool("solink")`. That is the GN
    equivalent of reassigning LD in a Makefile: it is the one place the linker
    choice is guaranteed to take effect, independent of how the project happens
    to spell its ldflags or its `extra_ldflags` arg.
    """
    rw = Rewriter(linker, bare_requires_context=False)
    out: List[str] = []
    in_flag_list = 0
    non_elf = bool(NON_ELF_TOOLCHAIN_RE.search(file))
    if non_elf and select_flags:
        rw.diags.append(Diag("info", "toolchain",
                             "non-ELF toolchain: linker selection left untouched", file, None))
    tool_name = None
    tool_depth = 0
    for i, ln in enumerate(text.split("\n"), 1):
        # Track which tool() block we are in: -B belongs in the link tools only,
        # never in tool("cxx") or an action's command.
        tm = GN_TOOL_RE.match(ln)
        if tm:
            tool_name, tool_depth = tm.group(1), 0
        if tool_name is not None:
            tool_depth += ln.count("{") - ln.count("}")
            if tool_depth <= 0 and not tm:
                tool_name = None

        in_link_tool = tool_name in ("link", "solink", "solink_module")
        m = GN_COMMAND_RE.match(ln) if (select_flags and not non_elf and in_link_tool) else None
        if m and select_flags not in ln and GN_DRIVER_RE.match(m.group(2)):
            out.append(f"{m.group(1)}{m.group(2)}{select_flags} {m.group(3)}")
            rw.diags.append(Diag("info", "link command",
                                 f'eld shim spliced into tool("{tool_name}")', file, i))
            continue

        m = GN_USE_LINKER_RE.match(ln)
        if m and m.group(4).rstrip(",") in ("true", "false"):
            want = "false"
            if m.group(4).rstrip(",") != want:
                out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{want}  # eldconv: was {m.group(4)}")
                rw.diags.append(Diag("info", m.group(2),
                                     "pinned to false; eld is selected through the shim", file, i))
                continue
            out.append(ln)
            continue
        if GN_LINKER_ASSERT_RE.match(ln):
            out.append(f"# eldconv: removed -> {ln.strip()}")
            rw.diags.append(Diag("warn", "assert", "linker assertion removed; "
                                 "review the feature it guards (usually LTO)", file, i))
            continue

        if GN_LDFLAGS_START_RE.match(ln):
            in_flag_list = 1
        elif in_flag_list:
            in_flag_list += ln.count("[") - ln.count("]")
            if in_flag_list <= 0:
                in_flag_list = 0

        if '"' not in ln:
            out.append(ln)
            continue

        # Convert each quoted element; drop elements that convert to nothing.
        removed = []

        def repl(sm: re.Match) -> str:
            body = sm.group(1)
            if not body.strip():
                return sm.group(0)
            new = _convert_gn_string(rw, body, file, i)
            if new.strip() == body.strip():
                return sm.group(0)
            if not new.strip():
                removed.append(body)
                return '""'
            return '"' + new + '"'

        new_ln = GN_STRING_RE.sub(repl, ln)
        if removed:
            # A string that is one operand of a `+` cannot simply vanish: the
            # rest of the expression (rebase_path(...), a $var) would be left
            # as a bare ldflags entry. Comment the whole statement out instead.
            if re.search(r'""\s*\+|\+\s*""', new_ln):
                rw.diags.append(Diag(
                    "warn", removed[0],
                    "linker selection built by concatenation; the whole statement "
                    "was commented out (eld is selected through the shim)", file, i))
                out.append(f"{ln[:len(ln) - len(ln.lstrip())]}# eldconv: removed -> {ln.strip()}")
                continue
            # An emptied element must leave the list entirely, not stay as "".
            new_ln = re.sub(r'""\s*,\s*', "", new_ln)
            new_ln = re.sub(r',\s*""(?=\s*[\],])', "", new_ln)
            new_ln = re.sub(r'\[\s*""\s*\]', "[]", new_ln)
            new_ln = re.sub(r'^(\s*)""\s*,?\s*$', "", new_ln)
            if not new_ln.strip():
                continue
        out.append(new_ln)
    return "\n".join(out), rw.diags


# A ninja rule name that links. `alink` is an archiver (ar), which takes no
# linker flags and no -B, so it is deliberately excluded.
LINK_RULE_RE = re.compile(r"(?:^|_)(?:link|solink|solink_module|dylink)(?:$|_)", re.I)
# The driver at the head of a ninja command, e.g. `$cc`, `${cxx}`, `clang++`.
# A command that runs a linker itself (`ld.lld -r -o $out $in`).
NINJA_DIRECT_LD_RE = re.compile(r"^(?:[\w./-]*/)?(?:ld(?:\.(?:bfd|gold|lld))?|mold)(?:-[\d.]+)?$")
NINJA_DRIVER_RE = re.compile(
    r'^(?:\$\{?(?:ld|cc|cxx|CC|CXX|link|linker)\}?'
    r'|(?:[\w./-]*/)?(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+))$')


def is_link_rule(name: str) -> bool:
    return bool(LINK_RULE_RE.search(name))


def rewrite_ninja_text(text: str, linker: str, file: str = "build.ninja",
                       select_flags: str = "", shim_ld: str = "") -> Tuple[str, List[Diag]]:
    """Convert a ninja file.

    Flags are translated in the `command` of every link rule and in any
    `ldflags` variable. `select_flags` (e.g. `-B/path/eld-bin/`) is spliced in
    after the driver of each link rule's command -- the same place, and for the
    same reason, as GN's `tool("link")`: a per-edge `ldflags` override would
    otherwise shadow a change made only at the top of the file.
    """
    rw = Rewriter(linker, bare_requires_context=False)
    out: List[str] = []
    rule = None
    for i, ln in enumerate(text.split("\n"), 1):
        m = NINJA_RULE_RE.match(ln)
        if m:
            rule = m.group(1)
            out.append(ln)
            continue
        # Indented lines belong to the rule above; anything else ends it.
        if ln and not ln[0].isspace():
            rule = None

        in_link_rule = bool(rule and is_link_rule(rule))
        cm = NINJA_COMMAND_RE.match(ln) if rule else None
        if cm and shim_ld:
            head = cm.group(2).split(None, 1)
            if head and NINJA_DIRECT_LD_RE.match(head[0]):
                body = rw.rewrite_line(head[1] if len(head) > 1 else "", file, i)
                rw.diags.append(Diag("info", f"rule {rule}",
                                     f"runs `{head[0]}` directly; now runs eld's shim", file, i))
                out.append(f"{cm.group(1)}{shim_ld} {body}".rstrip())
                continue
        if cm and not in_link_rule:
            cm = None
        if cm:
            body = rw.rewrite_line(cm.group(2), file, i)
            if select_flags and select_flags not in body:
                parts = body.split(None, 1)
                if parts and NINJA_DRIVER_RE.match(parts[0]):
                    body = f"{parts[0]} {select_flags}" + (f" {parts[1]}" if len(parts) > 1 else "")
                    rw.diags.append(Diag("info", f"rule {rule}",
                                         "eld shim spliced in after the driver", file, i))
                else:
                    rw.diags.append(Diag(
                        "warn", f"rule {rule}",
                        f"could not identify the linker driver in this command "
                        f"(starts with `{parts[0] if parts else ''}`); eld is NOT "
                        f"selected for this rule -- add `{select_flags}` by hand", file, i))
            out.append(cm.group(1) + body)
            continue

        if in_link_rule or NINJA_LDFLAGS_RE.match(ln):
            out.append(rw.rewrite_line(ln, file, i))
            continue
        out.append(ln)
    return "\n".join(out), rw.diags


ARGS_GN = """# Generated by eldconv -- append to your out/<dir>/args.gn, then re-run `gn gen`.
#
# eld itself is selected inside the toolchain's tool("link") command (see the
# rewritten toolchain files), which is the only place GN guarantees takes
# effect. These args just stop the rest of the build assuming lld/mold.
use_lld = false
use_mold = false

# LTO and ICF are lld features; a project may assert on them when use_lld is false.
use_thin_lto = false
is_official_build = false
use_icf = false
"""
