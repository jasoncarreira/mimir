"""Tests for the pending-update flag flow (mimir/update_on_start.py).

Covers:
- No flag → no-op (the common path)
- Flag present + install succeeds → exec called with re-exec argv,
  flag deleted, mimir_update_applied event logged
- Flag present + install fails → flag deleted (no loop), exec NOT
  called, mimir_update_failed event logged, function returns
- Flag with target_version → pip spec includes ``==<version>``
- Flag with include_prereleases → pip argv carries ``--pre``
- Malformed JSON in flag → treated as empty defaults, doesn't crash
- Empty file → same (bare ``touch`` works as approval signal)
- write_flag round-trip → the operator-side tool produces a file
  that the startup-side reader parses correctly
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir.update_on_start import (
    PendingUpdate,
    _install_spec,
    _read_flag,
    apply_pending_update,
    flag_path,
    write_flag,
)


# ─── flag_path / write_flag ─────────────────────────────────────────


def test_flag_path_under_dotmimir(tmp_path: Path) -> None:
    """The flag lives under ``<home>/.mimir/`` so it shares the
    home volume's persistence with the saga DB + metrics."""
    p = flag_path(tmp_path)
    assert p == tmp_path / ".mimir" / "pending-update.flag"


def test_write_flag_creates_parent_dir(tmp_path: Path) -> None:
    """A fresh home doesn't have ``.mimir/`` yet; write_flag creates
    it. Otherwise the operator approval would error on a new
    deployment."""
    assert not (tmp_path / ".mimir").exists()
    write_flag(tmp_path)
    assert (tmp_path / ".mimir" / "pending-update.flag").is_file()


def test_write_flag_roundtrip_defaults(tmp_path: Path) -> None:
    """Empty-args write produces a flag the reader parses with
    sensible defaults (target_version='' → latest, no --pre)."""
    write_flag(tmp_path)
    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == ""
    assert parsed.include_prereleases is False
    assert parsed.approved_at is not None


def test_write_flag_roundtrip_pinned_prerelease(tmp_path: Path) -> None:
    """Explicit version + pre-release flag carry through write → read."""
    write_flag(tmp_path, target_version="0.2.0rc1", include_prereleases=True)
    parsed = _read_flag(flag_path(tmp_path))
    assert parsed.target_version == "0.2.0rc1"
    assert parsed.include_prereleases is True


def test_read_flag_tolerates_empty_file(tmp_path: Path) -> None:
    """Bare ``touch <flag>`` is a valid operator approval — no JSON
    payload required."""
    path = flag_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    parsed = _read_flag(path)
    assert parsed.target_version == ""
    assert parsed.include_prereleases is False


def test_read_flag_tolerates_malformed_json(tmp_path: Path) -> None:
    """Garbage payload defaults to empty + logs a warning, doesn't
    crash startup."""
    path = flag_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json {")
    parsed = _read_flag(path)
    assert parsed.target_version == ""


# ─── _install_spec ──────────────────────────────────────────────────


def test_install_spec_empty_target_returns_bare_pkg() -> None:
    parsed = PendingUpdate(target_version="", include_prereleases=False, approved_at=None)
    assert _install_spec("mimir-agent", parsed) == "mimir-agent"


def test_install_spec_pinned_target_uses_equality() -> None:
    parsed = PendingUpdate(target_version="0.2.0", include_prereleases=False, approved_at=None)
    assert _install_spec("mimir-agent", parsed) == "mimir-agent==0.2.0"


# ─── apply_pending_update — no flag ─────────────────────────────────


def test_apply_no_flag_returns_false(tmp_path: Path) -> None:
    """Common path: no flag → no-op, returns False. Startup proceeds
    normally."""
    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))
    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is False
    assert events == []
    assert exec_called == []
    assert not flag_path(tmp_path).exists()


# ─── apply_pending_update — happy path ──────────────────────────────


def test_apply_happy_path_runs_pip_then_execs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag present, pip install succeeds → flag deleted,
    mimir_update_applied logged, exec called with re-exec argv."""
    write_flag(tmp_path, target_version="0.2.0")

    captured_argv: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        # subprocess.CompletedProcess shape
        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    # The function attempted an install (returned True even though our
    # stubbed exec doesn't actually replace the process).
    assert result is True

    # pip spec was right — pinned to 0.2.0, no --pre.
    assert captured_argv, "pip install was not invoked"
    argv = captured_argv[0]
    assert argv[:5] == [sys.executable, "-m", "pip", "install", "--upgrade"]
    assert "--pre" not in argv
    # --no-cache-dir forces a fresh index fetch so a flag-update right after
    # a release can't fail on pip's stale cached index (chainlink #295).
    assert "--no-cache-dir" in argv
    assert argv[-1] == "mimir-agent==0.2.0"

    # Events: starting + applied. failed should NOT have been logged.
    kinds = [e[0] for e in events]
    assert "mimir_update_starting" in kinds
    assert "mimir_update_applied" in kinds
    assert "mimir_update_failed" not in kinds

    # Flag was deleted post-install.
    assert not flag_path(tmp_path).exists()

    # Exec was called with the re-exec argv shape: [python, *original argv].
    assert len(exec_called) == 1
    executable, exec_argv = exec_called[0]
    assert executable == sys.executable
    assert exec_argv[0] == sys.executable


# ─── apply_pending_update — install failure ─────────────────────────


def test_apply_install_failure_clears_flag_no_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip install fails → flag deleted (no loop), no exec, function
    returns True (we did attempt) but startup proceeds on OLD code."""
    write_flag(tmp_path)

    def _fake_run(argv, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "ERROR: No matching distribution found for mimir-agent"
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is True  # an attempt happened
    assert not flag_path(tmp_path).exists()  # flag cleared, no loop
    assert exec_called == []  # no re-exec on failure

    kinds = [e[0] for e in events]
    assert "mimir_update_failed" in kinds
    assert "mimir_update_applied" not in kinds


def test_apply_install_timeout_clears_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip install hangs past _PIP_TIMEOUT_S → flag deleted, failed
    event logged, no exec."""
    write_flag(tmp_path)

    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[tuple[str, dict]] = []
    def _log(kind, **fields):
        events.append((kind, fields))

    exec_called: list[tuple] = []
    def _fake_exec(executable, argv):
        exec_called.append((executable, argv))

    result = apply_pending_update(tmp_path, _log, _exec=_fake_exec)

    assert result is True
    assert not flag_path(tmp_path).exists()
    assert exec_called == []
    assert "mimir_update_failed" in [e[0] for e in events]


# ─── --pre flag propagation ──────────────────────────────────────────


def test_apply_includes_pre_flag_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag with include_prereleases=True passes --pre to pip."""
    write_flag(tmp_path, target_version="0.2.0rc1", include_prereleases=True)

    captured_argv: list[list[str]] = []
    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    assert captured_argv
    argv = captured_argv[0]
    assert "--pre" in argv
    assert argv[-1] == "mimir-agent==0.2.0rc1"


# ─── env-var override for package name ───────────────────────────────


def test_apply_honors_pypi_package_name_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MIMIR_PYPI_PACKAGE_NAME env overrides the default ``mimir-agent``
    — for forks / pre-release channels."""
    write_flag(tmp_path)
    monkeypatch.setenv("MIMIR_PYPI_PACKAGE_NAME", "mimir-fork")

    captured_argv: list[list[str]] = []
    def _fake_run(argv, **kwargs):
        captured_argv.append(argv)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    assert captured_argv[0][-1] == "mimir-fork"


# ─── startup-events sidecar (the carry-over fix from PR #330 review) ─


def test_apply_writes_sidecar_with_install_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apply_pending_update`` runs BEFORE ``init_logger``; events
    must be persisted to a sidecar file so they can be drained into
    ``events.jsonl`` later by ``consume_startup_events``. Verifies
    that on the happy path, both ``mimir_update_starting`` and
    ``mimir_update_applied`` land in the sidecar."""
    write_flag(tmp_path, target_version="0.2.0")

    def _fake_run(argv, **kwargs):
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    sidecar = tmp_path / ".mimir" / "startup-events.jsonl"
    assert sidecar.is_file(), "sidecar was not written"
    lines = [json.loads(l) for l in sidecar.read_text().splitlines() if l.strip()]
    kinds = [e["type"] for e in lines]
    assert "mimir_update_starting" in kinds
    assert "mimir_update_applied" in kinds


def test_apply_writes_sidecar_on_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure path: ``mimir_update_failed`` lands in the sidecar so
    the algedonic block on next turn can surface the rollback."""
    write_flag(tmp_path)

    def _fake_run(argv, **kwargs):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "ERROR: ..."
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    sidecar = tmp_path / ".mimir" / "startup-events.jsonl"
    assert sidecar.is_file()
    lines = [json.loads(l) for l in sidecar.read_text().splitlines() if l.strip()]
    kinds = [e["type"] for e in lines]
    assert "mimir_update_starting" in kinds
    assert "mimir_update_failed" in kinds
    # Verify the failure event captured the rc + truncated stderr.
    failed = next(e for e in lines if e["type"] == "mimir_update_failed")
    assert failed["rc"] == 1
    assert "stderr_tail" in failed


@pytest.mark.asyncio
async def test_consume_startup_events_drains_into_async_logger(
    tmp_path: Path,
) -> None:
    """``consume_startup_events`` reads the sidecar, replays each
    event through the now-initialized async ``log_event``, and
    deletes the sidecar so subsequent restarts don't re-emit."""
    # Intentional: importing underscore-prefixed internals so the
    # test can drive the sidecar from the same path the production
    # ``apply_pending_update`` would. The leading underscore signals
    # "module-internal" — kept that way so tests can pin the wire
    # format, while production callers go through the public
    # ``apply_pending_update`` / ``consume_startup_events`` pair.
    from mimir.update_on_start import (
        _record_startup_event,
        _STARTUP_EVENTS_BASENAME,
        consume_startup_events,
    )

    # Simulate what apply_pending_update writes.
    _record_startup_event(
        tmp_path, "mimir_update_starting", spec="mimir-agent==0.2.0",
        include_pre=False,
    )
    _record_startup_event(
        tmp_path, "mimir_update_applied", spec="mimir-agent==0.2.0",
        approved_at="2026-05-24T22:00:00Z",
    )

    captured: list[tuple[str, dict]] = []

    async def _async_log(kind, **fields):
        captured.append((kind, fields))

    drained = await consume_startup_events(tmp_path, _async_log)

    assert drained == 2
    kinds = [c[0] for c in captured]
    assert kinds == ["mimir_update_starting", "mimir_update_applied"]
    # The ``ts`` field is stripped before replay — the real event
    # logger stamps its own timestamp.
    for kind, fields in captured:
        assert "ts" not in fields
    # Sidecar is gone after drain.
    sidecar = tmp_path / ".mimir" / _STARTUP_EVENTS_BASENAME
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_consume_startup_events_noop_when_no_sidecar(
    tmp_path: Path,
) -> None:
    """No sidecar (the common case — no install attempted on this
    restart) → returns 0, no async_log_event calls."""
    from mimir.update_on_start import consume_startup_events

    calls: list = []
    async def _async_log(*args, **kwargs):
        calls.append((args, kwargs))

    drained = await consume_startup_events(tmp_path, _async_log)
    assert drained == 0
    assert calls == []


@pytest.mark.asyncio
async def test_consume_startup_events_skips_malformed_lines(
    tmp_path: Path,
) -> None:
    """A corrupt sidecar line shouldn't block the rest of the drain.
    Robustness — the sidecar is written best-effort + the operator
    might inspect / edit it between boot and drain."""
    from mimir.update_on_start import (
        _STARTUP_EVENTS_BASENAME,
        consume_startup_events,
    )

    sidecar = tmp_path / ".mimir" / _STARTUP_EVENTS_BASENAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"type": "mimir_update_starting", "spec": "x"}) + "\n"
        + "this is not json\n"
        + json.dumps({"type": "mimir_update_applied", "spec": "x"}) + "\n"
    )

    captured: list[str] = []
    async def _async_log(kind, **fields):
        captured.append(kind)

    drained = await consume_startup_events(tmp_path, _async_log)
    # Two valid events drained; the malformed line skipped.
    assert drained == 2
    assert captured == ["mimir_update_starting", "mimir_update_applied"]


def test_apply_truncates_stale_sidecar_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense against the "drain succeeded but unlink failed on
    prior boot" failure mode: when ``apply_pending_update`` detects
    a fresh flag, it clears any leftover sidecar before writing the
    current boot's events. Without this, the prior boot's stale
    events would still be on disk and the next drain would replay
    both old + new — duplicate ``mimir_update_applied`` lines in
    ``events.jsonl``.

    Mimir-carreira nit 1 on PR #333 review.
    """
    # Plant a stale sidecar from a hypothetical prior boot.
    from mimir.update_on_start import _STARTUP_EVENTS_BASENAME
    sidecar = tmp_path / ".mimir" / _STARTUP_EVENTS_BASENAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"type": "mimir_update_applied", "spec": "STALE"}) + "\n"
    )

    # Write a fresh flag — simulates a new operator approval.
    write_flag(tmp_path, target_version="0.2.0")

    def _fake_run(argv, **kwargs):
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)

    # The stale "STALE" entry must be gone. Only the current boot's
    # events should be present.
    lines = [json.loads(l) for l in sidecar.read_text().splitlines() if l.strip()]
    specs = [e.get("spec") for e in lines]
    assert "STALE" not in specs, "stale sidecar entry leaked into current boot's drain"
    # The current boot's events ARE present.
    assert any(
        e.get("type") == "mimir_update_applied" and e.get("spec") == "mimir-agent==0.2.0"
        for e in lines
    )


def test_apply_does_not_touch_sidecar_when_no_flag(
    tmp_path: Path,
) -> None:
    """The truncate fires ONLY when a flag is present. A boot with
    no pending-update doesn't touch the sidecar — preserves the
    invariant that an existing sidecar represents work this boot
    actually did, and avoids spuriously deleting state if some
    other component started using the sidecar path."""
    from mimir.update_on_start import _STARTUP_EVENTS_BASENAME
    sidecar = tmp_path / ".mimir" / _STARTUP_EVENTS_BASENAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sentinel = json.dumps({"type": "sentinel", "from": "other-component"}) + "\n"
    sidecar.write_text(sentinel)

    # No flag — the no-op path.
    result = apply_pending_update(tmp_path, lambda *a, **k: None, _exec=lambda *a: None)
    assert result is False
    # Sidecar still has the sentinel content; truncate did not fire.
    assert sidecar.read_text() == sentinel


# ─── UpdateDigest / _compute_update_digest helpers ──────────────────


def test_update_digest_roundtrip(tmp_path: Path) -> None:
    """``UpdateDigest.to_dict`` / ``from_dict`` round-trip preserves all
    fields including the env_gaps tuples (which JSON serialises as lists
    and must be restored correctly)."""
    from mimir.update_on_start import UpdateDigest

    orig = UpdateDigest(
        prior_version="0.2.1",
        new_version="0.2.2",
        scheduler_delta=["issues-audit", "commitments-review"],
        skills_drift=["github"],
        env_gaps=[("weather", "OPENWEATHER_API_KEY"), ("ntfy", "NTFY_TOPIC")],
    )
    restored = UpdateDigest.from_dict(orig.to_dict())
    assert restored == orig
    assert isinstance(restored.env_gaps[0], tuple)


def test_update_digest_empty_roundtrip() -> None:
    """Empty digest serialises and restores correctly — the common case
    when an update installs nothing new."""
    from mimir.update_on_start import UpdateDigest

    orig = UpdateDigest(prior_version="0.2.1", new_version="0.2.2")
    restored = UpdateDigest.from_dict(orig.to_dict())
    assert restored == orig
    assert restored.scheduler_delta == []
    assert restored.skills_drift == []
    assert restored.env_gaps == []


def test_scheduler_delta_returns_missing_ticks(tmp_path: Path) -> None:
    """Tick names in the template but absent from the live scheduler.yaml
    are returned as the scheduler delta."""
    import yaml
    from mimir.update_on_start import _scheduler_delta

    template_path = tmp_path / "scheduler_template.yaml"
    live_path = tmp_path / "scheduler.yaml"

    template_path.write_text(
        yaml.dump([{"name": "heartbeat"}, {"name": "reflect"}, {"name": "issues-audit"}])
    )
    # Live scheduler is missing "issues-audit" — it shipped in this version
    live_path.write_text(
        yaml.dump([{"name": "heartbeat"}, {"name": "reflect"}])
    )

    # Patch _BUNDLED_SCHEDULER to point at our fixture template.
    import mimir.skill_defs as _sd
    orig = _sd._BUNDLED_SCHEDULER
    try:
        _sd._BUNDLED_SCHEDULER = template_path
        delta = _scheduler_delta(tmp_path)
    finally:
        _sd._BUNDLED_SCHEDULER = orig

    assert delta == ["issues-audit"]


def test_scheduler_delta_no_delta_when_live_has_all(tmp_path: Path) -> None:
    """When the live scheduler already contains every template tick,
    the delta is empty — no operator action needed."""
    import yaml
    from mimir.update_on_start import _scheduler_delta

    template_path = tmp_path / "scheduler_template.yaml"
    live_path = tmp_path / "scheduler.yaml"

    both = [{"name": "heartbeat"}, {"name": "reflect"}]
    template_path.write_text(yaml.dump(both))
    live_path.write_text(yaml.dump(both))

    import mimir.skill_defs as _sd
    orig = _sd._BUNDLED_SCHEDULER
    try:
        _sd._BUNDLED_SCHEDULER = template_path
        delta = _scheduler_delta(tmp_path)
    finally:
        _sd._BUNDLED_SCHEDULER = orig

    assert delta == []


def test_scheduler_delta_bundled_template_is_list_parseable(tmp_path: Path) -> None:
    """The actual bundled scheduler_template.yaml must be parseable as a top-level
    list of {name: ...} dicts — this test catches any schema drift in the template
    itself before it can silently break _scheduler_delta at runtime."""
    import yaml
    from mimir.skill_defs import _BUNDLED_SCHEDULER

    data = yaml.safe_load(_BUNDLED_SCHEDULER.read_text(encoding="utf-8"))
    assert isinstance(data, list), (
        f"scheduler_template.yaml must be a top-level list, got {type(data).__name__}"
    )
    names = [e["name"] for e in data if isinstance(e, dict) and "name" in e]
    assert len(names) > 0, "scheduler_template.yaml must have at least one named entry"
    # Live scheduler with an empty set (no entries) → all template ticks are delta.
    live_path = tmp_path / "scheduler.yaml"
    live_path.write_text("[]")
    from mimir.update_on_start import _scheduler_delta

    delta = _scheduler_delta(tmp_path)
    assert sorted(delta) == sorted(names)


def test_scheduler_delta_missing_files_returns_empty(tmp_path: Path) -> None:
    """If either file is absent (e.g. fresh install with no scheduler.yaml yet),
    return an empty list — don't crash."""
    from mimir.update_on_start import _scheduler_delta

    # Neither file exists in tmp_path.
    import mimir.skill_defs as _sd
    orig = _sd._BUNDLED_SCHEDULER
    try:
        _sd._BUNDLED_SCHEDULER = tmp_path / "nonexistent_template.yaml"
        delta = _scheduler_delta(tmp_path)
    finally:
        _sd._BUNDLED_SCHEDULER = orig

    assert delta == []


def _make_fake_bundled_root(tmp_path: Path, skills: dict[str, str]) -> Path:
    """Helper: create a fake _BUNDLED_ROOT directory with one SKILL.md per
    entry in *skills* (``{skill_name: skill_md_content}``). Returns the root."""
    root = tmp_path / "fake_pkg_skills"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in skills.items():
        d = root / name
        d.mkdir()
        (d / "SKILL.md").write_text(content)
    return root


def test_env_gaps_returns_missing_required_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled skill with a required env var absent from os.environ shows up
    in the env_gaps list.  Reads from the installed package root (_BUNDLED_ROOT),
    not the home-seeded .mimir_builtin_skills/."""
    import mimir.skill_defs as _sd
    from mimir.update_on_start import _env_gaps

    skill_md = (
        "---\n"
        "name: weather\n"
        "env:\n"
        "  required:\n"
        "    - name: OPENWEATHER_API_KEY\n"
        "      description: OpenWeatherMap API key\n"
        "---\n"
        "# Weather skill\n"
    )
    fake_root = _make_fake_bundled_root(tmp_path, {"weather": skill_md})
    orig = _sd._BUNDLED_ROOT
    try:
        _sd._BUNDLED_ROOT = fake_root
        monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
        gaps = _env_gaps(tmp_path)
    finally:
        _sd._BUNDLED_ROOT = orig

    assert ("weather", "OPENWEATHER_API_KEY") in gaps


def test_env_gaps_empty_when_all_required_vars_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all required env vars are present, env_gaps is empty."""
    import mimir.skill_defs as _sd
    from mimir.update_on_start import _env_gaps

    skill_md = (
        "---\n"
        "name: weather\n"
        "env:\n"
        "  required:\n"
        "    - name: OPENWEATHER_API_KEY\n"
        "---\n"
        "# Weather skill\n"
    )
    fake_root = _make_fake_bundled_root(tmp_path, {"weather": skill_md})
    orig = _sd._BUNDLED_ROOT
    try:
        _sd._BUNDLED_ROOT = fake_root
        monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key-123")
        gaps = _env_gaps(tmp_path)
    finally:
        _sd._BUNDLED_ROOT = orig

    assert gaps == []


def test_env_gaps_reads_package_not_home_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_env_gaps reads the installed package's bundled skills, not the
    home-seeded .mimir_builtin_skills/ copy.

    This is the staleness-prevention test: when a skill is added in a new
    package version with a required env var, _compute_update_digest (which
    runs pre-execv, before the new boot refreshes .mimir_builtin_skills/)
    must still surface it.  The home-seeded copy reflects the *old*
    version; only the package root reflects the *new* one.
    """
    import mimir.skill_defs as _sd
    from mimir.update_on_start import _env_gaps

    new_skill_md = (
        "---\n"
        "name: brand-new-skill\n"
        "env:\n"
        "  required:\n"
        "    - name: BRAND_NEW_SKILL_API_KEY\n"
        "      description: Key introduced in this update\n"
        "---\n"
        "# Brand-new skill (added in this version)\n"
    )
    # Package root HAS the new skill; home-seeded dir does NOT.
    fake_pkg_root = _make_fake_bundled_root(tmp_path, {"brand-new-skill": new_skill_md})
    # Deliberately leave .mimir_builtin_skills/ absent (simulates pre-execv state
    # where the home copy hasn't been refreshed from the new package yet).
    assert not (tmp_path / ".mimir_builtin_skills").exists()

    orig = _sd._BUNDLED_ROOT
    try:
        _sd._BUNDLED_ROOT = fake_pkg_root
        monkeypatch.delenv("BRAND_NEW_SKILL_API_KEY", raising=False)
        gaps = _env_gaps(tmp_path)
    finally:
        _sd._BUNDLED_ROOT = orig

    assert ("brand-new-skill", "BRAND_NEW_SKILL_API_KEY") in gaps, (
        "_env_gaps should read the installed package root, not the home-seeded "
        ".mimir_builtin_skills/ — the home copy is stale pre-execv and won't "
        "contain skills added in this update"
    )


def test_compute_update_digest_scheduler_delta(tmp_path: Path) -> None:
    """``_compute_update_digest`` assembles the delta from helper functions;
    scheduler_delta reflects ticks added in template but missing from live."""
    import yaml
    from mimir.update_on_start import _compute_update_digest

    template_path = tmp_path / "tpl.yaml"
    live_path = tmp_path / "scheduler.yaml"
    template_path.write_text(
        yaml.dump([{"name": "heartbeat"}, {"name": "issues-audit"}])
    )
    live_path.write_text(yaml.dump([{"name": "heartbeat"}]))

    import mimir.skill_defs as _sd
    orig = _sd._BUNDLED_SCHEDULER
    try:
        _sd._BUNDLED_SCHEDULER = template_path
        digest = _compute_update_digest(tmp_path, prior_version="0.2.1")
    finally:
        _sd._BUNDLED_SCHEDULER = orig

    assert digest.prior_version == "0.2.1"
    assert "issues-audit" in digest.scheduler_delta


def test_compute_update_digest_no_delta(tmp_path: Path) -> None:
    """When template and live scheduler are identical, scheduler_delta is empty."""
    import yaml
    from mimir.update_on_start import _compute_update_digest

    both = yaml.dump([{"name": "heartbeat"}])
    template_path = tmp_path / "tpl.yaml"
    (tmp_path / "scheduler.yaml").write_text(both)
    template_path.write_text(both)

    import mimir.skill_defs as _sd
    orig = _sd._BUNDLED_SCHEDULER
    try:
        _sd._BUNDLED_SCHEDULER = template_path
        digest = _compute_update_digest(tmp_path, prior_version="0.2.2")
    finally:
        _sd._BUNDLED_SCHEDULER = orig

    assert digest.scheduler_delta == []


# ─── consume_update_digest sidecar roundtrip ─────────────────────────


@pytest.mark.asyncio
async def test_update_digest_sidecar_roundtrip(tmp_path: Path) -> None:
    """``_write_update_digest_sidecar`` + ``consume_update_digest`` round-trip:
    writing a digest, then consuming it, emits a ``mimir_update_digest`` event
    with the correct field values and deletes the sidecar so a second restart
    doesn't re-emit."""
    from mimir.update_on_start import (
        UpdateDigest,
        _write_update_digest_sidecar,
        _UPDATE_DIGEST_BASENAME,
        consume_update_digest,
    )

    digest = UpdateDigest(
        prior_version="0.2.1",
        new_version="0.2.2",
        scheduler_delta=["issues-audit", "commitments-review"],
        skills_drift=["github"],
        env_gaps=[("weather", "OPENWEATHER_API_KEY")],
    )
    _write_update_digest_sidecar(tmp_path, digest)

    captured: list[tuple[str, dict]] = []

    async def _async_log(kind: str, **fields):  # type: ignore[misc]
        captured.append((kind, fields))

    drained = await consume_update_digest(tmp_path, _async_log)

    assert drained == 1
    assert len(captured) == 1
    kind, fields = captured[0]
    assert kind == "mimir_update_digest"
    assert fields["prior_version"] == "0.2.1"
    assert fields["new_version"] == "0.2.2"
    assert "issues-audit" in fields["scheduler_delta"]
    assert "github" in fields["skills_drift"]
    assert ("weather", "OPENWEATHER_API_KEY") in [
        tuple(g) for g in fields["env_gaps"]
    ]
    # Sidecar deleted after successful drain.
    sidecar = tmp_path / ".mimir" / _UPDATE_DIGEST_BASENAME
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_update_digest_consume_no_sidecar(tmp_path: Path) -> None:
    """When no sidecar file exists (common path — no update on this restart),
    ``consume_update_digest`` returns 0 and emits no events."""
    from mimir.update_on_start import consume_update_digest

    calls: list = []

    async def _async_log(*args, **kwargs):  # type: ignore[misc]
        calls.append((args, kwargs))

    drained = await consume_update_digest(tmp_path, _async_log)

    assert drained == 0
    assert calls == []


# ─── mimir_update_digest feedback renderer ───────────────────────────


def test_mimir_update_digest_render_all_surfaces() -> None:
    """When all three diff surfaces are non-empty the renderer produces
    a one-liner covering scheduler delta, skills drift, and env gaps."""
    from mimir.feedback import _render_event_line

    line = _render_event_line(
        "mimir_update_digest",
        {
            "prior_version": "0.2.1",
            "new_version": "0.2.2",
            "scheduler_delta": ["issues-audit", "commitments-review"],
            "skills_drift": ["github"],
            "env_gaps": [["weather", "OPENWEATHER_API_KEY"]],
        },
    )

    assert "[mimir v0.2.1→0.2.2]" in line
    assert "scheduler +2 tick(s)" in line
    assert "issues-audit" in line
    assert "skills drifted: github" in line
    assert "env missing: weather/OPENWEATHER_API_KEY" in line


def test_mimir_update_digest_render_nothing_required() -> None:
    """When all three diff surfaces are empty the renderer returns the
    'nothing requires action' fallback — the update was a no-op."""
    from mimir.feedback import _render_event_line

    line = _render_event_line(
        "mimir_update_digest",
        {
            "prior_version": "0.2.2",
            "new_version": "0.2.3",
            "scheduler_delta": [],
            "skills_drift": [],
            "env_gaps": [],
        },
    )

    assert "[mimir v0.2.2→0.2.3]" in line
    assert "nothing requires action" in line


# ─── emit_version_bump_digest (chainlink #363) ──────────────────────


class _Drift:
    """Minimal stand-in for SkillDriftResult (only fields the digest uses)."""
    def __init__(self, name: str, is_clean: bool) -> None:
        self.name = name
        self.is_clean = is_clean


def _capture_log():
    calls: list = []

    async def _async_log(kind, **fields):
        calls.append((kind, fields))

    return _async_log, calls


def _patch_digest_inputs(monkeypatch, *, current, drift):
    """Pin the version + skill-drift + zero out scheduler/env deltas so the
    digest reflects only the injected drift.

    Pins BOTH version sources to ``current``: the gate reads
    ``_current_version()`` while ``_compute_update_digest`` reads the digest's
    ``new_version`` from ``importlib.metadata``. In production both derive from
    the same installed version; pin them together here so the test doesn't
    couple to whatever the repo's current pyproject version happens to be."""
    monkeypatch.setattr("mimir.update_on_start._current_version", lambda: current)
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: current)
    monkeypatch.setattr("mimir.skill_install.detect_skill_drift", lambda home, *a, **k: drift)
    monkeypatch.setattr("mimir.update_on_start._scheduler_delta", lambda home: [])
    monkeypatch.setattr("mimir.update_on_start._env_gaps", lambda home: [])


@pytest.mark.asyncio
async def test_version_bump_emits_digest_with_drift(tmp_path, monkeypatch):
    from mimir.update_on_start import (
        emit_version_bump_digest,
        _write_last_booted_version,
        _read_last_booted_version,
    )
    _write_last_booted_version(tmp_path, "0.2.11")
    _patch_digest_inputs(
        monkeypatch, current="0.2.12",
        drift=[_Drift("social-cli", False), _Drift("github-poller", True)],
    )
    log, calls = _capture_log()
    n = await emit_version_bump_digest(tmp_path, log, already_drained=False)
    assert n == 1
    assert len(calls) == 1
    kind, fields = calls[0]
    assert kind == "mimir_update_digest"
    assert fields["prior_version"] == "0.2.11"
    assert fields["new_version"] == "0.2.12"
    assert fields["skills_drift"] == ["social-cli"]  # clean skill excluded
    # baseline advanced so it won't re-fire next boot
    assert _read_last_booted_version(tmp_path) == "0.2.12"


@pytest.mark.asyncio
async def test_version_bump_first_boot_baselines_silently(tmp_path, monkeypatch):
    from mimir.update_on_start import emit_version_bump_digest, _read_last_booted_version
    _patch_digest_inputs(monkeypatch, current="0.2.12", drift=[_Drift("social-cli", False)])
    log, calls = _capture_log()
    n = await emit_version_bump_digest(tmp_path, log, already_drained=False)
    assert n == 0 and calls == []           # no prior baseline → no notice
    assert _read_last_booted_version(tmp_path) == "0.2.12"


@pytest.mark.asyncio
async def test_version_bump_skipped_when_already_drained(tmp_path, monkeypatch):
    from mimir.update_on_start import emit_version_bump_digest, _write_last_booted_version, _read_last_booted_version
    _write_last_booted_version(tmp_path, "0.2.11")
    _patch_digest_inputs(monkeypatch, current="0.2.12", drift=[_Drift("social-cli", False)])
    log, calls = _capture_log()
    n = await emit_version_bump_digest(tmp_path, log, already_drained=True)
    assert n == 0 and calls == []           # self-update already surfaced it
    assert _read_last_booted_version(tmp_path) == "0.2.12"  # still re-baselined


@pytest.mark.asyncio
async def test_version_bump_noop_when_unchanged(tmp_path, monkeypatch):
    from mimir.update_on_start import emit_version_bump_digest
    _patch_digest_inputs(monkeypatch, current="0.2.12", drift=[_Drift("social-cli", False)])
    from mimir.update_on_start import _write_last_booted_version
    _write_last_booted_version(tmp_path, "0.2.12")
    log, calls = _capture_log()
    n = await emit_version_bump_digest(tmp_path, log, already_drained=False)
    assert n == 0 and calls == []


@pytest.mark.asyncio
async def test_version_bump_silent_when_no_actionable_delta(tmp_path, monkeypatch):
    """Version bumped but nothing drifted / no scheduler/env delta → no notice."""
    from mimir.update_on_start import emit_version_bump_digest, _write_last_booted_version
    _write_last_booted_version(tmp_path, "0.2.11")
    _patch_digest_inputs(monkeypatch, current="0.2.12", drift=[])
    log, calls = _capture_log()
    n = await emit_version_bump_digest(tmp_path, log, already_drained=False)
    assert n == 0 and calls == []


@pytest.mark.asyncio
async def test_version_bump_fires_once(tmp_path, monkeypatch):
    from mimir.update_on_start import emit_version_bump_digest, _write_last_booted_version
    _write_last_booted_version(tmp_path, "0.2.11")
    _patch_digest_inputs(monkeypatch, current="0.2.12", drift=[_Drift("social-cli", False)])
    log, calls = _capture_log()
    assert await emit_version_bump_digest(tmp_path, log, already_drained=False) == 1
    # second boot on the same version → baseline matches → silent
    assert await emit_version_bump_digest(tmp_path, log, already_drained=False) == 0
    assert len(calls) == 1
