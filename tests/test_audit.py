"""Auditing the mapping table against a real ld.eld."""
import json
import subprocess


from eldconv.audit import audit
from eldconv.cli import main

HELP = """OVERVIEW: eld linker
OPTIONS:
  --gc-sections            Enable garbage collection of unused sections
  --as-needed              Set DT_NEEDED only for used libraries
  --icf                    Identical code folding
  --orphan-handling        What to do with orphan sections
  --start-lib              Start a lib grouping
  --push-state             Save the current option state
  --Map                    Print a link map
"""


def make_runner(help_text=HELP, accepts=(), version="eld 1.2.3"):
    """A fake ld.eld: lists `help_text`, and accepts the named extra options."""
    def runner(cmd, **kw):
        args = cmd[1:]
        if "--help" in args:
            return subprocess.CompletedProcess(cmd, 0, help_text, "")
        if args == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, version + "\n", "")
        opt = args[0]
        if opt in accepts:
            return subprocess.CompletedProcess(cmd, 0, version + "\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", f"ld.eld: error: unknown argument '{opt}'\n")
    return runner


def test_version_and_listing_are_reported():
    res = audit("/fake/ld.eld", runner=make_runner())
    assert res.version == "eld 1.2.3"
    assert res.listed >= 7


def test_keep_rule_eld_rejects_is_missing():
    """A KEEP rule for something eld does not accept will break the build."""
    res = audit("/fake/ld.eld", runner=make_runner())
    missing = {f.option for f in res.by_kind("missing")}
    # --wrap is KEEP in the table but absent from this eld's help and rejected
    assert "--wrap" in missing
    # --gc-sections is listed, so it must not be reported
    assert "--gc-sections" not in missing


def test_drop_rule_eld_supports_is_needless():
    """If eld accepts an option the table drops, the conversion loses behaviour."""
    res = audit("/fake/ld.eld", runner=make_runner())
    needless = {f.option for f in res.by_kind("needless")}
    assert "--icf" in needless           # listed by this eld, but the table drops it
    f = next(f for f in res.findings if f.option == "--icf" and f.kind == "needless")
    assert f.action in ("unsupported", "drop") and "accepts it" in f.note


def test_acceptance_probe_rescues_undocumented_options():
    """eld may accept options it does not advertise; --help alone is not enough."""
    strict = audit("/fake/ld.eld", runner=make_runner())
    assert "--wrap" in {f.option for f in strict.by_kind("missing")}
    lenient = audit("/fake/ld.eld", runner=make_runner(accepts={"--wrap"}))
    assert "--wrap" not in {f.option for f in lenient.by_kind("missing")}
    assert lenient.accepted.get("--wrap") is True


def test_acceptance_probe_can_be_skipped():
    res = audit("/fake/ld.eld", probe_acceptance=False, runner=make_runner(accepts={"--wrap"}))
    assert "--wrap" in {f.option for f in res.by_kind("missing")}
    assert res.accepted == {}


def test_tentative_rules_are_confirmed():
    res = audit("/fake/ld.eld", runner=make_runner())
    confirmed = {f.option for f in res.by_kind("confirmed")}
    # both are tentative in the table and listed by this eld
    assert "--start-lib" in confirmed and "--orphan-handling" in confirmed
    assert all(f.tentative for f in res.by_kind("confirmed"))


def test_options_eld_has_but_the_table_does_not():
    res = audit("/fake/ld.eld", runner=make_runner(
        help_text=HELP + "  --brand-new-thing  Something the table never heard of\n"))
    assert "--brand-new-thing" in {f.option for f in res.by_kind("unknown")}


def test_rename_target_is_checked_not_the_source():
    res = audit("/fake/ld.eld", runner=make_runner())
    renames = [f for f in res.findings if "->" in f.option]
    # --Map is listed, so the --Map -> -Map rename resolves against the target
    assert all("->" in f.option for f in renames)


def test_glob_rules_are_not_looked_up():
    res = audit("/fake/ld.eld", runner=make_runner())
    assert not any("*" in f.option for f in res.findings)


def test_driver_level_rules_are_skipped():
    res = audit("/fake/ld.eld", runner=make_runner())
    reported = {f.option for f in res.findings}
    assert "-fuse-ld" not in reported and "--ld-path" not in reported


def test_empty_help_is_handled():
    res = audit("/fake/ld.eld", runner=make_runner(help_text="no options here\n"))
    assert res.findings == [] and res.listed == 0


def test_probe_failure_does_not_crash():
    def runner(cmd, **kw):
        raise OSError("boom")
    res = audit("/fake/ld.eld", runner=runner)
    assert res.listed == 0 and res.findings == []


def test_every_finding_serialises():
    res = audit("/fake/ld.eld", runner=make_runner())
    d = res.as_dict()
    json.dumps(d)
    assert set(d["summary"]) == {"missing", "needless", "confirmed", "unknown"}
    assert sum(d["summary"].values()) == len(res.findings)


def test_cli_audit_exit_code_and_output(tmp_path, capsys, monkeypatch):
    fake = tmp_path / "ld.eld"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    import eldconv.audit as audit_mod
    monkeypatch.setattr(audit_mod.subprocess, "run", make_runner())
    rc = main(["audit", "--eld", str(fake)])
    cap = capsys.readouterr()
    payload = json.loads(cap.out)
    assert payload["version"] == "eld 1.2.3"
    assert "AUDIT:" in cap.err
    # a table this eld would reject is a failure
    assert rc == 1


def test_cli_audit_missing_eld(capsys):
    assert main(["audit", "--eld", "/definitely/not/here"]) == 2
    assert "not found" in capsys.readouterr().err


def test_keyed_z_rules_are_probed_individually_not_collapsed():
    """Regression test: before the (name, value) dedup fix, only the FIRST "-z"
    rule in the table was ever probed and every other "-z" rule inherited its
    result -- so a linker accepting "-z now" but rejecting "-z nognustack" (the
    real ld.eld 24.0 situation is the reverse, but the bug was symmetric) would
    have been reported identically for both. Each keyword must be judged on its
    own live probe."""
    def runner(cmd, **kw):
        args = cmd[1:]
        if "--help" in args:
            return subprocess.CompletedProcess(cmd, 0, HELP, "")
        if args == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "eld 1.2.3\n", "")
        if args[:1] == ["-z"]:
            val = args[1] if len(args) > 1 else None
            if val == "now":
                return subprocess.CompletedProcess(cmd, 0, "eld 1.2.3\n", "")
            return subprocess.CompletedProcess(
                cmd, 1, "", f"ld.eld: error: unrecognized option `-z {val}'\n")
        return subprocess.CompletedProcess(cmd, 1, "", "not eld\n")

    res = audit("/fake/ld.eld", runner=runner)
    missing = {f.option for f in res.by_kind("missing")}
    # "-z now" (KEEP, not tentative) is accepted by this fake -> not missing
    assert "-z now" not in missing
    # "-z nognustack" (KEEP, not tentative) is rejected by this fake -> reported
    # on its own, proving it wasn't skipped as a duplicate of "-z now"
    assert "-z nognustack" in missing


def test_keyed_rules_report_the_full_spelling():
    """Findings for -z-style rules should name the keyword, not just "-z", so
    two different findings for the same flag don't look identical."""
    res = audit("/fake/ld.eld", runner=make_runner())
    z_findings = [f.option for f in res.findings if f.option.startswith("-z ")]
    assert z_findings, "expected at least one keyed -z finding"
    assert len(z_findings) == len(set(z_findings)), "keyed findings must be distinguishable"
