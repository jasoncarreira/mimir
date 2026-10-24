"""Discriminating regressions for turn-content review follow-ups."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import _shell_reads_home_content, ensure_turn_scratch
from mimir.turn_stores import close_turn_stores, sweep_turn_stores, turn_store, wipe_turn_stores


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("command", ["cat *.jsonl", "head -20 *", "rg x ."])
def test_remembered_content_store_cwd_taints_without_named_operand(tmp_path, monkeypatch, tool, command):
    from mimir.tools import extra

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(extra, "_effective_shell_cwd", lambda: tmp_path / "logs")
    assert _shell_reads_home_content({"command": command}, tool_name=tool)


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
def test_narrow_search_from_repository_stays_clean(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    assert not _shell_reads_home_content(
        {"command": "rg foo src", "cwd": str(tmp_path / "checkout")}, tool_name=tool,
    )


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize(("command", "expected"), [
    ('grep -rn "foo .', True),
    ("rg 'unterminated", True),
    ("ag 'unterminated", True),
    ("egrep 'unterminated", True),
    ("fgrep 'unterminated", True),
    ('echo "x', False),
])
def test_malformed_search_quoting_fails_closed_outside_home(
    tmp_path, monkeypatch, tool, command, expected,
):
    import shlex

    home, checkout = tmp_path / "home", tmp_path / "checkout"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    # Prove the fixture reaches the malformed-input fallback; neither cwd nor
    # a named content path can independently account for the positive verdict.
    with pytest.raises(ValueError):
        list(shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n"))
    assert _shell_reads_home_content(
        {"command": command, "cwd": str(checkout)}, tool_name=tool,
    ) is expected


def _install_research_manifest(home, name):
    manifest = home / "skills" / name / "pollers.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"pollers": [{
        "name": name, "command": "python3 poller.py", "cron": "0 * * * *",
        "authority": {"profile": "research", "tier": "scoped-with-provenance",
                      "capabilities": ["open_proposal", "submit_proposal"],
                      "scoped_roots": ["state"]},
    }]}))


def test_startup_sweeps_nested_research_drafts_but_preserves_operator_audit(tmp_path):
    _install_research_manifest(tmp_path, "arxiv-agent-memory")
    draft = tmp_path / "state/pollers/arxiv-agent-memory/research-123/notes-draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("ephemeral")
    cursor = draft.parent / "cursor.json"
    cursor.write_text('{"seen": ["id"]}')
    audit = tmp_path / "state/pollers/gmail-poller/triage-dropped.jsonl"
    audit.parent.mkdir(parents=True)
    payload = '{"message_id":"operator-data","subject":"keep","answers":{}}\n'
    audit.write_text(payload)
    sweep_turn_stores(tmp_path)
    assert not draft.exists()
    assert cursor.read_text() == '{"seen": ["id"]}'
    assert audit.read_text() == payload


@pytest.mark.asyncio
async def test_retained_tool_thread_cannot_allocate_after_final_wipe(tmp_path, monkeypatch):
    from threading import Event

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    ctx = SimpleNamespace(turn_id="retained-thread", turn_scratch_path=ensure_turn_scratch(tmp_path, "retained-thread"))
    entered, resume = Event(), Event()
    ctx_handle = set_current_turn(ctx)
    try:
        assert turn_store("large_tool_results")

        def late_writer():
            entered.set()
            assert resume.wait(10)
            return turn_store("large_tool_results")

        worker = asyncio.create_task(asyncio.to_thread(late_writer))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            close_turn_stores(ctx)
            wipe_turn_stores(tmp_path, ctx.turn_id)
        finally:
            resume.set()
        assert await worker is None
        assert not (tmp_path / "large_tool_results" / ctx.turn_id).exists()
    finally:
        reset_current_turn(ctx_handle)


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("operand", [
    "$MIMIR_HOME", '"$MIMIR_HOME"', "${MIMIR_HOME}", '"${MIMIR_HOME}"',
    "~", "$HOME", "${HOME}", '"$HOME"',
])
@pytest.mark.parametrize("cd_first", [False, True])
def test_workspace_recursive_search_of_home_spellings_taints(
    tmp_path, monkeypatch, tool, operand, cd_first,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    command = (f"cd {operand} && grep -rn foo ." if cd_first
               else f"grep -rn foo {operand}")
    assert _shell_reads_home_content({"command": command, "cwd": str(tmp_path / "checkout")}, tool_name=tool)


@pytest.mark.parametrize("target", ["home", ".", "..", "../sibling", "home/src", "home/"])
def test_absolute_home_operand_distinguishes_ancestors_and_descendants(tmp_path, monkeypatch, target):
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    path = str(tmp_path / target)
    expected = target in {"home", ".", "..", "home/", "home/src"}
    # Normalize only the fixture path, as a shell would do for literal paths.
    path = str(Path(path).resolve())
    assert _shell_reads_home_content({"command": f'rg foo "{path}"', "cwd": str(tmp_path / "checkout")}) is expected


@pytest.mark.parametrize("command", ["grep --color foo .", "grep --line-number foo ."])
def test_nonrecursive_grep_long_options_are_not_recursive(tmp_path, monkeypatch, command):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert not _shell_reads_home_content({"command": command, "cwd": str(tmp_path)})


def test_logs_path_component_independent_of_filename_and_recursion(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    assert _shell_reads_home_content({"command": "cat logs/2026-10-24.log", "cwd": str(tmp_path / "checkout")})
    assert not _shell_reads_home_content({"command": "echo ordinary logs", "cwd": str(tmp_path / "checkout")})


@pytest.mark.parametrize("invalid", ["..", ".", "../other", "a/b", "/absolute", ""])
def test_wipe_invalid_turn_id_never_enters_destructive_helper(tmp_path, monkeypatch, invalid):
    from mimir import turn_stores

    keep = tmp_path / "memory/keep.md"
    keep.parent.mkdir()
    keep.write_text("operator owned")
    (tmp_path / "large_tool_results/valid-turn").mkdir(parents=True)
    calls = []

    def safety_interlock(fd, name):
        # Even a mutation removing the production guard cannot delete anything.
        calls.append(name)
        assert name == "valid-turn", "invalid id reached destructive helper"
        return 0

    monkeypatch.setattr(turn_stores, "_remove_entry", safety_interlock)
    wipe_turn_stores(tmp_path, invalid)
    assert calls == []
    assert keep.read_text() == "operator owned"
    wipe_turn_stores(tmp_path, "valid-turn")
    assert calls == ["valid-turn"]


@pytest.mark.parametrize("linked", ["state/pollers/github-ci-watch/logs", "state/pollers", ".mimir"])
def test_startup_sweep_skips_symlinked_containers(tmp_path, caplog, linked):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (outside / "keep").write_text("operator")
    _install_research_manifest(home, "papers")
    link = home / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with caplog.at_level(logging.WARNING, logger="mimir.turn_stores"):
        sweep_turn_stores(home)
    assert (outside / "keep").read_text() == "operator"
    assert link.is_symlink()
    assert any(record.name == "mimir.turn_stores" for record in caplog.records)


@pytest.mark.parametrize("area", ["ci", "research"])
def test_startup_sweep_permission_error_continues_other_sections(tmp_path, monkeypatch, caplog, area):
    from mimir import turn_stores

    _install_research_manifest(tmp_path, "papers")
    root = tmp_path / ("state/pollers/github-ci-watch/logs" if area == "ci" else "state/pollers/papers")
    blocked = root / "unreadable"
    blocked.mkdir(parents=True)
    (blocked / "notes-draft.md").write_text("keep on refusal")
    real_open = os.open
    observed = []

    def denied(path, flags, *args, **kwargs):
        if path == "unreadable":
            observed.append(path)
            raise PermissionError("test-owned unreadable child")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(turn_stores.os, "open", denied)
    with caplog.at_level(logging.WARNING, logger="mimir.turn_stores"):
        sweep_turn_stores(tmp_path)
    assert observed
    assert (blocked / "notes-draft.md").read_text() == "keep on refusal"
    assert (tmp_path / ".mimir/turn-stores-v1").is_file()
    assert any(record.name == "mimir.turn_stores" for record in caplog.records)


def test_nonresearch_poller_drafts_survive_restart(tmp_path):
    _install_research_manifest(tmp_path, "papers")
    paths = [tmp_path / f"state/pollers/{name}/drafts/keep.md"
             for name in ("papers", "worklink-ready-queue")]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text("draft")
    sweep_turn_stores(tmp_path)
    assert not paths[0].exists()
    assert paths[1].read_text() == "draft"


def test_symlinked_home_uses_resolved_turn_store(tmp_path, monkeypatch):
    home, link = tmp_path / "home", tmp_path / "alias"
    home.mkdir()
    link.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(link))
    token = set_current_turn(SimpleNamespace(turn_id="resolved", turn_scratch_path=ensure_turn_scratch(home, "resolved")))
    try:
        assert turn_store("large_tool_results") == "/large_tool_results/resolved"
        assert (home / "large_tool_results/resolved").is_dir()
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
async def test_refused_summary_store_does_not_write_shared_history(tmp_path, monkeypatch, async_call):
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain_core.messages import HumanMessage
    from mimir._deepagents_summarization import install_offload_traceback_logging_patch
    from mimir.readonly_backend import WriteGuardBackend

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    backend = WriteGuardBackend(tmp_path, ["conversation_history"])
    install_offload_traceback_logging_patch()
    middleware = SummarizationMiddleware(model="openai:test", backend=backend)
    token = set_current_turn(SimpleNamespace(turn_id="refused", turn_scratch_path=None))
    try:
        messages = [HumanMessage(content="untrusted content")]
        pointer = (await middleware._aoffload_to_backend(backend, messages, "session") if async_call
                   else middleware._offload_to_backend(backend, messages, "session"))
        assert pointer is None
        assert not (tmp_path / "conversation_history").exists()
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True])
async def test_refused_large_result_store_stays_inline_within_turn(tmp_path, monkeypatch, async_call):
    from langchain_core.messages import ToolMessage
    from mimir.readonly_backend import MimirFilesystemMiddleware, WriteGuardBackend

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    backend = WriteGuardBackend(tmp_path, ["large_tool_results"])
    middleware = MimirFilesystemMiddleware(backend=backend, tool_token_limit_before_evict=1)
    message = ToolMessage(content="untrusted content " * 100, tool_call_id="large")
    token = set_current_turn(SimpleNamespace(turn_id="refused", turn_scratch_path=None))
    try:
        result, evicted = (await middleware._aprocess_large_message(message, backend) if async_call
                           else middleware._process_large_message(message, backend))
        assert result is message and not evicted
        assert not (tmp_path / "large_tool_results").exists()
    finally:
        reset_current_turn(token)


def test_summary_label_sidecar_is_turn_owned_and_wiped(tmp_path, monkeypatch):
    from mimir import _deepagents_summarization as offload
    from mimir.models import InformationFlowLabels, SourceLabel

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    source = SourceLabel(principal="operator", domain="test", resource_id="fixture",
                         bridge_instance="test", sensitivity="internal",
                         authorized_principals=frozenset({"operator"}),
                         integrity="trusted", integrity_effect="informational")
    labels = InformationFlowLabels(sources=(source,))
    ctx = SimpleNamespace(turn_id="labels-turn", auth_context=SimpleNamespace(ifc_labels=labels))
    token = set_current_turn(ctx)
    try:
        path = tmp_path / "conversation_history/labels-turn/session.md"
        path.parent.mkdir(parents=True)
        path.write_text("summary")
        offload._record_offload(str(path), None, None, hashlib.sha256(path.read_bytes()).hexdigest())
        sidecars = list((tmp_path / ".mimir/conversation_history_labels").rglob("*.json"))
        assert len(sidecars) == 1
        assert sidecars[0].parent == tmp_path / ".mimir/conversation_history_labels/labels-turn"
        assert offload.read_offload_labels(str(path)) == (source,)
        wipe_turn_stores(tmp_path, ctx.turn_id)
        assert not sidecars[0].exists() and not path.exists()
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_ci_log_delivery_skips_nonregular_files(tmp_path, kind):
    from mimir.turn_stores import move_ci_logs_to_turn

    source = tmp_path / "state/pollers/github-ci-watch/logs/42-101.log"
    source.parent.mkdir(parents=True)
    keep = tmp_path / "keep"
    keep.write_text("operator")
    if kind == "symlink":
        source.symlink_to(keep)
    elif kind == "directory":
        source.mkdir()
    else:
        os.mkfifo(source)
    mode = source.lstat().st_mode
    scratch = ensure_turn_scratch(tmp_path, "ci-consumer")
    content = f"Read CI log: {source}"
    event = SimpleNamespace(service_principal="poller:github-ci-watch", content=content)
    assert move_ci_logs_to_turn(tmp_path, event, scratch) == ()
    assert source.lstat().st_mode == mode
    assert event.content == content
    assert keep.read_text() == "operator"
    assert not list((scratch / "ci-logs").iterdir())


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("command", [
    "grep -n -r foo .", "grep -i -r foo .", "grep -E -r foo .",
    "grep -e x -r .", "grep --include=*.md -r foo .",
    "grep --color -r foo .", "grep foo -r $MIMIR_HOME",
    "grep -n -r foo ~", "cd ~ && grep -n -r foo .",
    "egrep -r foo .", "fgrep -r foo .", "grep -d recurse foo .",
    "grep --directories=recurse foo .", "grep --directories recurse foo .",
    "grep --recursive foo .", "grep -Rn 'foo bar' . | head",
    "grep -rn foo . 2>/dev/null", "grep -rn foo .\necho done",
])
def test_recursive_grep_flags_anywhere_in_segment(tmp_path, monkeypatch, tool, command):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert _shell_reads_home_content({"command": command, "cwd": str(tmp_path)}, tool_name=tool)


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("operand", [
    "$MIMIR_HOME/*", "~/*", "{home}/*", "$MIMIR_HOME/.", "{home}/.",
    "$MIMIR_HOME/state", "$MIMIR_HOME/attachments", "~/attachments/fetch-cache",
    "$MIMIR_HOME/large_tool_results", "${HOME}/state/pollers", '"${MIMIR_HOME}/state"',
    "~/projects/x", "{home}/memory/../state",
])
def test_recursive_home_paths_use_self_authored_allowlist(tmp_path, monkeypatch, tool, operand):
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    command = "grep -rn foo " + operand.format(home=home) if "{home}" in operand else "grep -rn foo " + operand
    assert _shell_reads_home_content({"command": command, "cwd": str(tmp_path / "checkout")}, tool_name=tool)


@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
@pytest.mark.parametrize("relative", ["state", "attachments", "attachments/fetch-cache", "projects"])
def test_recursive_cwd_under_home_is_not_implicitly_trusted(tmp_path, monkeypatch, tool, relative):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert _shell_reads_home_content({"command": "grep -rn foo .", "cwd": str(tmp_path / relative)}, tool_name=tool)


@pytest.mark.parametrize("root", ["memory", "docs", "skills", "prompts", ".mimir_builtin_skills"])
@pytest.mark.parametrize("tool", ["shell_exec", "bash_async"])
def test_recursive_search_all_operands_self_authored_stays_clean(tmp_path, monkeypatch, root, tool):
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    for cwd, command in [
        (home, f"rg foo {root}/"),
        (home / root, "grep -n -r foo ."),
        (tmp_path / "checkout", f"grep --recursive foo $MIMIR_HOME/{root} ~/memory"),
        (home, f"grep -e foo -r {root}/*"),
        (home, f"rg --files {root}/"),
    ]:
        assert not _shell_reads_home_content({"command": command, "cwd": str(cwd)}, tool_name=tool)
    assert _shell_reads_home_content({"command": f"rg foo ~/memory ~/state", "cwd": str(home)}, tool_name=tool)


@pytest.mark.parametrize("command", [
    "rg foo src && echo ~", "echo ~ | rg foo", "grep --color foo .",
    "grep --line-number foo .", "echo grep -r ~", "rg foo /other/checkouts",
])
def test_search_segment_does_not_consume_unrelated_operands(tmp_path, monkeypatch, command):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    assert not _shell_reads_home_content({"command": command, "cwd": str(tmp_path / "checkout")})


def test_manifest_with_nonresearch_authority_preserves_drafts(tmp_path):
    from mimir.pollers import discover_pollers
    from mimir.turn_stores import _research_poller_names

    _install_research_manifest(tmp_path, "papers")
    manifest = tmp_path / "skills/nonresearch/pollers.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"pollers": [{
        "name": "nonresearch", "command": "python3 poller.py", "cron": "0 * * * *",
        "authority": {"profile": "github", "tier": "code-execution",
                      "capabilities": ["read_file"], "scoped_roots": ["state"]},
    }]}))
    paths = [tmp_path / f"state/pollers/{name}/drafts/notes.md" for name in ("papers", "nonresearch")]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text("draft")
    configs = discover_pollers(tmp_path / "skills", state_root=tmp_path / "state/pollers")
    assert {config.name for config in configs} == {"papers", "nonresearch"}
    assert all(config.authority is not None for config in configs)
    assert _research_poller_names(tmp_path) == {"papers"}
    sweep_turn_stores(tmp_path)
    assert not paths[0].exists()
    assert paths[1].read_text() == "draft"


@pytest.mark.parametrize("area", ["ci", "research"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError, NotADirectoryError])
def test_entry_refusal_does_not_skip_later_entries(tmp_path, monkeypatch, caplog, area, error_type):
    from mimir import turn_stores

    _install_research_manifest(tmp_path, "papers")
    root = tmp_path / ("state/pollers/github-ci-watch/logs" if area == "ci" else "state/pollers/papers")
    root.mkdir(parents=True)
    first, second = root / "first-draft.md", root / "second-draft.md"
    first.write_text("refused")
    second.write_text("remove")
    real_listdir, real_remove = os.listdir, turn_stores._remove_entry
    attempts = []

    def ordered(fd):
        entries = real_listdir(fd)
        if set(entries) == {first.name, second.name}:
            return [first.name, second.name]
        return entries

    def refuse_first(fd, name):
        if name in {first.name, second.name}:
            attempts.append(name)
        if name == first.name:
            # Pin the full OSError boundary, not only PermissionError: an outer
            # handler can otherwise hide a narrowed per-entry catch while the
            # later draft remains unswept.
            raise error_type("test-owned first entry refusal")
        return real_remove(fd, name)

    monkeypatch.setattr(turn_stores.os, "listdir", ordered)
    monkeypatch.setattr(turn_stores, "_remove_entry", refuse_first)
    with caplog.at_level(logging.WARNING, logger="mimir.turn_stores"):
        sweep_turn_stores(tmp_path)
    assert attempts == [first.name, second.name]
    assert first.read_text() == "refused"
    assert not second.exists()
    assert any(record.name == "mimir.turn_stores" for record in caplog.records)
