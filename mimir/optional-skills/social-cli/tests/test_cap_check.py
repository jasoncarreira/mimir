"""Tests for ``cap_check.py`` — the archive/ledger cross-check.

The two properties worth pinning are both about *placement*, because this
script resolves both the agent home and its ``count.py`` delegate from
``__file__``. It was written when it sat at the skill root; moving it into
``scripts/`` changes both, and getting either wrong degrades the cap check
silently — it still exits 0 and prints a number, just a wrong one.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def fresh_cap_check():
    sys.modules.pop("cap_check", None)
    return importlib.import_module("cap_check")


def test_count_delegate_sits_beside_this_script() -> None:
    """``count.py`` must be resolvable from cap_check's own directory.

    ``count_ledger`` runs ``Path(__file__).parent / "count.py"``. When a stray
    root-level copy of ``count.py`` was removed from an install, this lookup
    started failing — and the failure surfaced as ``ledger=0`` with exit 0, so
    the cap check silently became archive-only and would under-report posts
    against the daily cap.
    """
    module = fresh_cap_check()
    beside = Path(module.__file__).resolve().parent / "count.py"
    assert beside.is_file(), f"count.py must sit beside cap_check.py, looked at {beside}"


def test_home_fallback_resolves_to_the_agent_home(monkeypatch) -> None:
    """With MIMIR_HOME unset, the fallback must reach the home, not skills/.

    The script lives at ``<home>/skills/social-cli/scripts/cap_check.py``, so the
    walk up is four levels. At three — correct when it sat at the skill root —
    it resolves to ``skills/`` and every archive glob silently finds nothing.
    """
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    module = fresh_cap_check()
    home = module._home()
    script = Path(module.__file__).resolve()
    assert home == script.parents[3]
    assert (home / "skills").is_dir() or home.name != "skills", (
        f"fallback resolved to {home}, which looks like the skills dir rather "
        "than the agent home"
    )


def test_mimir_home_env_wins_over_the_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    module = fresh_cap_check()
    assert module._home() == tmp_path


def test_cap_and_post_class_actions_match_the_guideline() -> None:
    """The cap and the action set are the contract this script enforces."""
    module = fresh_cap_check()
    assert module.CAP == 5
    assert module.POST_CLASS_ACTIONS == {"post", "reply", "thread", "quote"}


# --------------------------------------------------------------------------
# Ledger delegate: a failed read must not be reported as a count of zero.
#
# This is the property the script exists for. When `count.py` went missing
# from an install, `count_ledger` converted every failure into `0` — a
# perfectly valid count — so the check printed `effective=0 / 5`, exited 0,
# and handed back five posts of headroom it had never verified.
# --------------------------------------------------------------------------


def _fake_run(*, returncode=0, stdout="", raises=None):
    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode,
            stdout=stdout, stderr="boom",
        )
    return run


def test_ledger_is_unavailable_when_the_delegate_is_missing(monkeypatch, tmp_path):
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=FileNotFoundError("no count.py")))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_the_delegate_times_out(monkeypatch, tmp_path):
    module = fresh_cap_check()
    timeout = subprocess.TimeoutExpired(cmd="count.py", timeout=10)
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=timeout))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_the_delegate_exits_nonzero(monkeypatch, tmp_path):
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stdout=""))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_the_delegate_prints_non_integer(monkeypatch, tmp_path):
    """Malformed stdout is an unknown count, not zero posts."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="Traceback (most recent call last)"))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def _payload(**over):
    """A well-formed count.py --json report, overridable per test."""
    base = {"count": 0, "unreadable": 0, "scanned": 1,
            "damagedRecords": 0, "stateDirs": 1}
    base.update(over)
    return json.dumps(base)


def test_ledger_returns_the_delegate_count_on_success(monkeypatch, tmp_path):
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(count=3)))
    assert module.count_ledger(tmp_path, "2026-08-20") == 3


def test_a_real_zero_from_the_delegate_is_still_zero(monkeypatch, tmp_path):
    """The fix must not turn a genuine zero into `unavailable`."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(count=0)))
    assert module.count_ledger(tmp_path, "2026-08-20") == 0


def test_ledger_is_unavailable_when_no_state_directory_was_found(monkeypatch, tmp_path):
    """A zero drawn from nowhere is not a zero.

    No `social-cli-*` directory means no evidence the poller has ever run
    here, so there is nothing behind the number.
    """
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(stateDirs=0, scanned=0)))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_an_initialised_state_dir_with_no_ledger_yet_is_a_real_zero(monkeypatch, tmp_path):
    """A poller that has run and written nothing must not block the first post.

    The framework creates the poller's state directory on first run, before
    any dispatch, so this is genuinely "nothing sent yet" rather than
    "looking in the wrong place". Failing closed here would deadlock: the
    ledger only appears after a post, and the post needs the guard to pass.
    """
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(scanned=0, stateDirs=1)))
    assert module.count_ledger(tmp_path, "2026-08-20") == 0


def test_ledger_is_unavailable_when_the_report_admits_damage(monkeypatch, tmp_path):
    """Belt and braces: damage in the payload, even with a clean exit code."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(count=2, unreadable=1)))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_records_were_uninterpretable(monkeypatch, tmp_path):
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=_payload(count=2, damagedRecords=1)))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_the_report_omits_state_dirs(monkeypatch, tmp_path):
    """An older count.py cannot report this, so it must fail closed."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(
        stdout=json.dumps({"count": 3, "unreadable": 0, "scanned": 1})))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_ledger_is_unavailable_when_the_delegate_prints_a_bare_integer(monkeypatch, tmp_path):
    """The plain-text path is no longer the contract; it carries no evidence."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="3\n"))
    assert module.count_ledger(tmp_path, "2026-08-20") is None


# --------------------------------------------------------------------------
# main(): what the agent actually reads.
# --------------------------------------------------------------------------


def _patch_sources(monkeypatch, module, *, archive=0, unreadable=0, ledger=0):
    monkeypatch.setattr(module, "count_archive", lambda home, today: (archive, unreadable))
    monkeypatch.setattr(module, "count_ledger", lambda home, today: ledger)
    monkeypatch.setattr(module, "_home", lambda: Path("/nonexistent"))


def test_main_fails_closed_when_the_ledger_is_unavailable(monkeypatch, capsys):
    """No number, and a nonzero exit — an outage must not read as headroom."""
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=0, ledger=None)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.LEDGER_UNAVAILABLE
    assert rc != 0
    assert "ledger=unavailable" in out
    assert "effective=unknown" in out
    assert "effective=0" not in out


def test_main_fails_closed_even_when_the_archive_has_entries(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=2, ledger=None)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.LEDGER_UNAVAILABLE
    assert "archive=2" in out
    assert "effective=unknown" in out


def test_main_reports_the_max_as_effective(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=1, ledger=2)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "archive=1 ledger=2 effective=2" in out


def test_main_agreeing_sources_exit_zero(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=3, ledger=3)
    assert module.main() == 0
    assert "effective=3" in capsys.readouterr().out


def test_main_flags_divergence_over_one(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=4, ledger=1)
    assert module.main() == 2
    assert "effective=4" in capsys.readouterr().out


def test_main_tolerates_divergence_of_one(monkeypatch, capsys):
    """Ledger lag of a single entry is expected, not a smell."""
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=2, ledger=1)
    assert module.main() == 0


def test_main_surfaces_unreadable_archive_files(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=1, unreadable=2, ledger=1)
    rc = module.main()
    out = capsys.readouterr().out
    assert "archive_unreadable=2" in out
    assert rc == module.ARCHIVE_UNREADABLE


def test_main_fails_closed_when_an_archive_file_is_unreadable(monkeypatch, capsys):
    """The regression for the reported case: 0 / 0 / one unparseable file.

    A malformed archive can be the only record of a dispatched post — that is
    the whole reason the archive is cross-checked, since the ledger write is
    the thing that can go missing. Reading both zeros as "no posts today"
    would hand back the full cap on the strength of a file we failed to open.
    """
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=0, unreadable=1, ledger=0)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.ARCHIVE_UNREADABLE
    assert rc != 0
    assert "effective=unknown" in out
    assert "effective=0" not in out
    assert "archive_unreadable=1" in out


def test_main_fails_closed_on_unreadable_archive_despite_a_readable_ledger(
    monkeypatch, capsys
):
    """A readable ledger is not a substitute for an unparseable archive."""
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=2, unreadable=1, ledger=2)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.ARCHIVE_UNREADABLE
    assert "effective=unknown" in out
    assert "effective=2" not in out
    assert "ledger=2" in out


def test_ledger_unavailability_is_reported_ahead_of_archive_damage(
    monkeypatch, capsys
):
    """Both sources broken: name the canonical one, since it gates the fix."""
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=0, unreadable=1, ledger=None)
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.LEDGER_UNAVAILABLE
    assert "ledger=unavailable" in out
    assert "effective=unknown" in out


def test_main_omits_the_unreadable_field_when_clean(monkeypatch, capsys):
    module = fresh_cap_check()
    _patch_sources(monkeypatch, module, archive=1, unreadable=0, ledger=1)
    module.main()
    assert "archive_unreadable" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Archive parsing.
# --------------------------------------------------------------------------


def _write_archive(home: Path, name: str, doc) -> Path:
    d = home / "state" / "pollers" / "social-cli-bsky" / "outbox_archive"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(yaml.safe_dump(doc))
    return path


def test_archive_counts_post_class_actions(tmp_path):
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-20T10-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "a"}}, {"reply": {"text": "b"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (2, 0)


def test_archive_accepts_the_bare_list_shape(tmp_path):
    """Older dispatches stored a bare list rather than a `dispatch:` mapping."""
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-20T10-00-00-000Z_outbox-bsky.yaml",
                   [{"thread": {"posts": []}}, {"quote": {"text": "c"}}])
    assert module.count_archive(tmp_path, "2026-08-20") == (2, 0)


def test_archive_excludes_dry_runs(tmp_path):
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-20T10-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "a", "dryRun": True}},
                                 {"post": {"text": "b"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 0)


def test_archive_excludes_non_post_class_actions(tmp_path):
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-20T10-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"like": {"uri": "x"}}, {"follow": {"did": "y"}},
                                 {"post": {"text": "a"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 0)


def test_archive_ignores_other_days(tmp_path):
    module = fresh_cap_check()
    _write_archive(tmp_path, "2026-08-19T10-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "yesterday"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 0)


def test_archive_accepts_the_compact_date_prefix(tmp_path):
    module = fresh_cap_check()
    _write_archive(tmp_path, "20260820T100000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "a"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 0)


def test_archive_reports_unreadable_files_rather_than_skipping_silently(tmp_path):
    module = fresh_cap_check()
    d = tmp_path / "state" / "pollers" / "social-cli-bsky" / "outbox_archive"
    d.mkdir(parents=True)
    (d / "2026-08-20T10-00-00-000Z_outbox-bsky.yaml").write_text("{[not: valid: yaml")
    _write_archive(tmp_path, "2026-08-20T11-00-00-000Z_outbox-bsky.yaml",
                   {"dispatch": [{"post": {"text": "a"}}]})
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 1)


# --------------------------------------------------------------------------
# End-to-end against the real count.py delegate.
#
# The mocked tests above pin cap_check's handling of a failed delegate. These
# run the actual subprocess, because the fail-open they cover lived *inside*
# count.py: it caught unreadable and malformed ledgers, skipped them, printed
# the partial total, and exited 0. Nothing in cap_check could see that, so a
# damaged ledger holding the only record of a post still established headroom.
# --------------------------------------------------------------------------


def _write_ledger(home: Path, name: str, text: str) -> Path:
    d = home / "state" / "pollers" / "social-cli-e2e"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(text, encoding="utf-8")
    return path


def _ledger_entry(ts: str) -> dict:
    return {"action": "post", "platform": "bsky", "timestamp": ts}


@pytest.fixture
def e2e_env(monkeypatch):
    """count.py resolves state from STATE_DIR first, so clear it."""
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("MIMIR_HOME", raising=False)


def test_end_to_end_real_delegate_counts_a_valid_ledger(e2e_env, tmp_path):
    """Positive control: without this, the failure test could pass for any reason."""
    module = fresh_cap_check()
    _write_ledger(
        tmp_path, "sent_ledger-bsky.yaml",
        yaml.safe_dump([_ledger_entry("2026-08-20T01:00:00Z"),
                        _ledger_entry("2026-08-20T02:00:00Z")]),
    )
    assert module.count_ledger(tmp_path, "2026-08-20") == 2


def test_end_to_end_real_delegate_reports_a_malformed_ledger_as_unavailable(
    e2e_env, tmp_path
):
    """A ledger that will not parse must not come back as a smaller count."""
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", "{[ not: valid: yaml")
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_end_to_end_a_malformed_ledger_hides_a_real_post(e2e_env, tmp_path):
    """The reported scenario, end to end.

    Two ledger files: one readable and empty, one malformed that holds the
    only record of a post. The old behaviour counted 0 from the readable
    file, skipped the other, and returned 0 — indistinguishable from a quiet
    day, and worth five posts of headroom.
    """
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", yaml.safe_dump([]))
    _write_ledger(tmp_path, "sent_ledger-bsky-archive.yaml", "{[ malformed")
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_end_to_end_an_unreadable_ledger_is_unavailable(e2e_env, tmp_path):
    """Unreadable is the OSError arm, distinct from the YAMLError arm."""
    module = fresh_cap_check()
    path = _write_ledger(tmp_path, "sent_ledger-bsky.yaml",
                         yaml.safe_dump([_ledger_entry("2026-08-20T01:00:00Z")]))
    path.chmod(0o000)
    try:
        result = module.count_ledger(tmp_path, "2026-08-20")
    finally:
        path.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root bypasses the permission bit, so nothing is unreadable")
    assert result is None


def test_end_to_end_an_empty_ledger_is_a_genuine_zero(e2e_env, tmp_path):
    """An empty file is not damage — it must stay a real zero, not unknown."""
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", "")
    assert module.count_ledger(tmp_path, "2026-08-20") == 0


def test_end_to_end_a_scalar_ledger_is_unavailable(e2e_env, tmp_path):
    """Syntactically valid YAML, structurally not a ledger."""
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", "garbage\n")
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_end_to_end_a_mistyped_entries_container_is_unavailable(e2e_env, tmp_path):
    """`entries: {}` — parses, yields nothing, must not read as zero posts.

    The live ledger nests under `entries:`, so this is the shape a format
    change or a bad write would actually produce.
    """
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", "entries: {}\n")
    assert module.count_ledger(tmp_path, "2026-08-20") is None


def test_end_to_end_the_production_ledger_shape_still_counts(e2e_env, tmp_path):
    """Positive control in the live format, through the real subprocess."""
    module = fresh_cap_check()
    _write_ledger(tmp_path, "sent_ledger-bsky.yaml", yaml.safe_dump({
        "entries": [
            {"action": "post", "platform": "bsky",
             "timestamp": "2026-08-20T01:00:00.000Z", "dryRun": False},
            {"action": "reply", "platform": "bsky",
             "timestamp": "2026-08-20T02:00:00.000Z", "dryRun": False},
        ],
    }))
    assert module.count_ledger(tmp_path, "2026-08-20") == 2


# --------------------------------------------------------------------------
# Structurally invalid archives — the mirror of the ledger validation.
#
# Fixing this for the ledger and not the archive was the same asymmetry as
# fixing the ledger fail-open and not the archive one, two revisions earlier.
# Both sources now get identical treatment: I/O damage, parse damage,
# unrecognized structure, and a missing directory all make the count a floor.
#
# Shapes verified against the 1539 deployed archive files before tightening:
# every one is a dict whose `dispatch` is a list of dicts, and four carry an
# extra `notifications` key alongside it, so sibling keys must stay legal.
# --------------------------------------------------------------------------


def _write_archive_raw(home: Path, name: str, text: str) -> Path:
    d = home / "state" / "pollers" / "social-cli-bsky" / "outbox_archive"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(text, encoding="utf-8")
    return path


_TODAY = "2026-08-20T10-00-00-000Z_outbox-bsky.yaml"


def test_archive_scalar_is_unreadable(tmp_path):
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "garbage\n")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 1)


def test_archive_mapping_without_dispatch_is_unreadable(tmp_path):
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "unexpected:\n  - post: {}\n")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 1)


def test_archive_mistyped_dispatch_container_is_unreadable(tmp_path):
    """`dispatch:` as a mapping rather than a list — the reported case."""
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "dispatch:\n  post:\n    text: a\n")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 1)


def test_archive_list_holding_a_non_entry_is_unreadable(tmp_path):
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "- post:\n    text: a\n- just-a-string\n")
    count, unreadable = module.count_archive(tmp_path, "2026-08-20")
    assert unreadable == 1


def test_archive_empty_file_is_a_real_zero(tmp_path):
    """An empty archive parses to None and must not be flagged as damage."""
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 0)


def test_archive_empty_dispatch_is_a_real_zero(tmp_path):
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "dispatch: []\n")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 0)


def test_archive_empty_list_is_a_real_zero(tmp_path):
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "[]\n")
    assert module.count_archive(tmp_path, "2026-08-20") == (0, 0)


def test_archive_sibling_keys_alongside_dispatch_are_legal(tmp_path):
    """Four deployed archives carry `notifications` next to `dispatch`."""
    module = fresh_cap_check()
    _write_archive(tmp_path, _TODAY, {
        "dispatch": [{"post": {"text": "a"}}],
        "notifications": [],
    })
    assert module.count_archive(tmp_path, "2026-08-20") == (1, 0)


def test_main_fails_closed_on_a_structurally_invalid_archive(monkeypatch, capsys, tmp_path):
    """End to end: the archive is the only evidence and the ledger is missing.

    This is the whole scenario in one test — a dispatched post recorded only
    in an archive we cannot interpret, with no ledger row to corroborate it.
    Reported as `effective=0` it would be worth the full daily cap.
    """
    module = fresh_cap_check()
    _write_archive_raw(tmp_path, _TODAY, "dispatch:\n  post:\n    text: a\n")
    monkeypatch.setattr(module, "_home", lambda: tmp_path)
    monkeypatch.setattr(module, "count_ledger", lambda home, today: 0)
    monkeypatch.setattr(module, "_today_str", lambda: "2026-08-20")
    rc = module.main()
    out = capsys.readouterr().out
    assert rc == module.ARCHIVE_UNREADABLE
    assert "effective=unknown" in out
    assert "effective=0" not in out
    assert "archive_unreadable=1" in out
