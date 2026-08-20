"""Tests for ``cap_check.py`` — the archive/ledger cross-check.

The two properties worth pinning are both about *placement*, because this
script resolves both the agent home and its ``count.py`` delegate from
``__file__``. It was written when it sat at the skill root; moving it into
``scripts/`` changes both, and getting either wrong degrades the cap check
silently — it still exits 0 and prints a number, just a wrong one.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

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


def test_ledger_returns_the_delegate_count_on_success(monkeypatch, tmp_path):
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="3\n"))
    assert module.count_ledger(tmp_path, "2026-08-20") == 3


def test_a_real_zero_from_the_delegate_is_still_zero(monkeypatch, tmp_path):
    """The fix must not turn a genuine zero into `unavailable`."""
    module = fresh_cap_check()
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="0\n"))
    assert module.count_ledger(tmp_path, "2026-08-20") == 0


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
    module.main()
    assert "archive_unreadable=2" in capsys.readouterr().out


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
