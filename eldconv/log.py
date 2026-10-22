"""Verbosity-aware progress reporting.

Levels
  0  quiet   -- only the final summary (the default)
  1  -v      -- the decisions: what was detected and why, which files changed,
                what was generated, which commands ran
  2  -vv     -- every file scanned and every flag decision, including the
                unchanged ones

Progress goes to stderr so that stdout stays machine-readable (`eldconv detect`
and `eldconv check` print JSON there).
"""
from __future__ import annotations

import sys
import time
from typing import IO, Iterable, Optional

QUIET = 0
VERBOSE = 1
TRACE = 2


class Log:
    def __init__(self, level: int = QUIET, stream: Optional[IO[str]] = None,
                 color: Optional[bool] = None):
        self.level = level
        self.stream = stream if stream is not None else sys.stderr
        if color is None:
            color = getattr(self.stream, "isatty", lambda: False)()
        self.color = bool(color)
        self._t0 = time.monotonic()
        self._counts = {"scanned": 0, "rewritten": 0, "unchanged": 0}

    # -- plumbing ------------------------------------------------------------
    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _emit(self, prefix: str, msg: str, code: str = "") -> None:
        stamp = f"[{time.monotonic() - self._t0:6.2f}s] "
        line = f"{stamp}{prefix} {msg}" if prefix else f"{stamp}{msg}"
        print(self._paint(line, code) if code else line, file=self.stream)

    @property
    def enabled(self) -> bool:
        return self.level >= VERBOSE

    @property
    def tracing(self) -> bool:
        return self.level >= TRACE

    # -- levels --------------------------------------------------------------
    def step(self, msg: str) -> None:
        """A phase boundary: detect, rewrite, generate, check."""
        if self.level >= VERBOSE:
            self._emit("==", msg, "1")

    def info(self, msg: str) -> None:
        if self.level >= VERBOSE:
            self._emit(" *", msg)

    def detail(self, msg: str) -> None:
        """Per-file / per-flag noise; only at -vv."""
        if self.level >= TRACE:
            self._emit("  -", msg, "2")

    def warn(self, msg: str) -> None:
        """Shown even when quiet: the user needs to know."""
        self._emit(" !", msg, "33")

    def error(self, msg: str) -> None:
        self._emit(" x", msg, "31")

    def finding(self, level: str, msg: str) -> None:
        """A problem or warning `check` found and will list in its report: shown as it is
        found only at -v, so a quiet run prints it once, in the report."""
        if self.level >= VERBOSE:
            (self.error if level == "error" else self.warn)(msg)

    def diag(self, level: str, msg: str) -> None:
        """A converter diagnostic.

        These are collected in the report and printed by the CLI when it is not
        running verbose, so streaming them here would double up. Only shown at
        -v and above.
        """
        if self.level < VERBOSE:
            return
        if level == "error":
            self.error(msg)
        elif level == "warn":
            self.warn(msg)
        else:
            self.detail(msg)

    # -- helpers used by the pipeline ---------------------------------------
    def file_scanned(self, rel: str, changed: bool, n_diags: int = 0) -> None:
        self._counts["scanned"] += 1
        if changed:
            self._counts["rewritten"] += 1
            self.info(f"rewrote {rel}" + (f" ({n_diags} change(s))" if n_diags else ""))
        else:
            self._counts["unchanged"] += 1
            self.detail(f"unchanged {rel}")

    def build_output(self, line: str) -> None:
        """One line of build output, as it arrives (-v)."""
        if self.level >= VERBOSE:
            print(self._paint(f"   | {line}", "2"), file=self.stream, flush=True)

    def command(self, argv: Iterable[str], cwd: Optional[str] = None) -> None:
        line = " ".join(str(a) for a in argv)
        self.info(f"run: {line}" + (f"   (cwd {cwd})" if cwd else ""))

    def counts(self) -> dict:
        return dict(self._counts)

    def summary(self) -> None:
        if self.level >= VERBOSE:
            c = self._counts
            self.step(f"scanned {c['scanned']} build file(s): "
                      f"{c['rewritten']} rewritten, {c['unchanged']} unchanged")


NULL_LOG = Log(QUIET)
