from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.github_withhold import (
    WithheldItem, partition, placeholder, sanitize_login, sanitize_timestamp,
    sanitize_url, summarise,
)
from tests.withhold_probe import OUTSIDER_MARKER, assert_marker_absent


def _candidate(kind: str, author: str) -> dict[str, object]:
    return {
        "kind": kind, "user": author, "repository": "owner/repo",
        "created_at": "2026-10-10T12:34:56Z",
        "html_url": "https://github.com/owner/repo/issues/42#issuecomment-1",
        "title": OUTSIDER_MARKER, "body": OUTSIDER_MARKER,
        "path": OUTSIDER_MARKER, "diff": OUTSIDER_MARKER,
    }


def test_partition_keeps_only_attested_authors_and_omits_all_content() -> None:
    items = [
        _candidate("issue", "collaborator"),
        _candidate("comment", "outsider"),
        _candidate("review", "app[bot]"),
        _candidate("pr_body", "unknown"),
    ]
    verdicts = {"collaborator": True, "outsider": False, "app[bot]": False, "unknown": None}
    queried: list[str] = []

    def verdict(author: str) -> bool | None:
        queried.append(author)
        return verdicts[author]

    kept, withheld = partition(items, lambda item: str(item["user"]), verdict)
    assert kept == items[:1]
    assert queried == list(verdicts)
    assert [item.reason for item in withheld] == [
        "non_collaborator", "bot_not_allowlisted", "attestation_unavailable",
    ]
    assert placeholder(withheld[0]) == {
        "kind": "comment", "author": "outsider", "created_at": "2026-10-10T12:34:56Z",
        "html_url": "https://github.com/owner/repo/issues/42#issuecomment-1",
        "reason": "non_collaborator", "withheld": True,
    }
    assert summarise(withheld) == {"withheld": 3, "by_kind": {
        "comment": 1, "review": 1, "pr_body": 1,
    }}
    assert_marker_absent([placeholder(item) for item in withheld], summarise(withheld))
    with pytest.raises(FrozenInstanceError):
        withheld[0].kind = "issue"  # type: ignore[misc]


@pytest.mark.parametrize("verdict", [1, "yes"])
def test_partition_withholds_truthy_non_bool_attestations(verdict: object) -> None:
    candidate = _candidate("comment", "outsider")
    kept, withheld = partition(
        [candidate], lambda item: str(item["user"]),
        lambda _: verdict,  # type: ignore[arg-type,return-value]
    )
    assert kept == []
    assert len(withheld) == 1
    assert withheld[0].reason == "non_collaborator"
    assert placeholder(withheld[0])["withheld"] is True
    assert_marker_absent([placeholder(item) for item in withheld])


@pytest.mark.parametrize("verdict", [1, "yes"])
def test_truthy_attestation_mutant_fails_regression(
    monkeypatch: pytest.MonkeyPatch, verdict: object,
) -> None:
    import mimir.github_withhold as withhold

    source = Path(withhold.__file__).read_text(encoding="utf-8")
    assert source.count("if verdict is True:") == 1
    namespace = dict(vars(withhold))
    exec(compile(source.replace("if verdict is True:", "if verdict:"),
                 str(withhold.__file__), "exec"), namespace)
    monkeypatch.setitem(globals(), "partition", namespace["partition"])
    with pytest.raises(AssertionError):
        test_partition_withholds_truthy_non_bool_attestations(verdict)


@pytest.mark.parametrize("login,expected", [
    ("evil<@everyone>", "<invalid-login>"),
    ("a" * 40, "<invalid-login>"),
    ("a\u202eb", "<invalid-login>"),
    ("@someone", "<invalid-login>"),
    ("app[bot]", "app[bot]"),
    ("Valid-User", "Valid-User"),
    ("a" * 39, "a" * 39),
])
def test_sanitize_login(login: str, expected: str) -> None:
    assert sanitize_login(login) == expected


@pytest.mark.parametrize("url", [
    "https://github.com/another/repo/issues/42",
    "https://github.com/owner/another/issues/42",
    "https://github.com.evil/owner/repo/issues/42",
    "javascript:alert(1)",
    "https://github.com/owner/repo/issues/42?q=secret",
    "https://github.com/owner/repo/issues/42#evil<script>",
    "https://github.com/owner/repo/issues/42/extra",
])
def test_sanitize_url_rejects_other_origins_repos_and_extra_text(url: str) -> None:
    assert sanitize_url(url, "owner/repo") is None


def test_sanitize_url_keeps_only_exact_expected_repo() -> None:
    url = "https://github.com/owner/repo/pull/12#discussion_r123"
    assert sanitize_url(url, "owner/repo") == url
    assert sanitize_url(url, "owner/other") is None
    assert sanitize_url(url, "owner/repo|other") is None


@pytest.mark.parametrize("value", [
    "2026-10-10 12:34:56Z", "2026-10-10T12:34:56+00:00",
    "2026-10-10T12:34:56Z injected", "2026-10-10T12:34Z",
    "2026-13-10T12:34:56Z", "2026-10-10T25:34:56Z",
])
def test_sanitize_timestamp_rejects_malformed(value: str) -> None:
    assert sanitize_timestamp(value) is None


def test_partition_sanitizes_metadata_and_direct_placeholder_cannot_emit_text() -> None:
    candidate = _candidate("issue", "evil<@everyone>")
    candidate.update({"created_at": OUTSIDER_MARKER, "html_url": "https://github.com/other/repo/issues/42"})
    _, [item] = partition([candidate], lambda i: str(i["user"]), lambda _: False)
    assert placeholder(item) == {
        "kind": "issue", "author": "<invalid-login>", "created_at": None,
        "html_url": None, "reason": "non_collaborator", "withheld": True,
    }
    direct = WithheldItem("issue", "evil<@everyone>", OUTSIDER_MARKER, OUTSIDER_MARKER, "non_collaborator")
    assert placeholder(direct)["author"] == "<invalid-login>"
    assert placeholder(direct)["created_at"] is None
    assert placeholder(direct)["html_url"] is None
    assert_marker_absent(placeholder(direct))


@pytest.mark.parametrize("blob", [
    lambda: {"outer": [{"inner": OUTSIDER_MARKER}]},
    lambda: ["safe", [OUTSIDER_MARKER]],
    lambda: json.dumps({"events": [{"content": OUTSIDER_MARKER}]}),
    lambda: SimpleNamespace(messages=[OUTSIDER_MARKER]),
])
def test_marker_probe_detects_nested_and_serialized_content(blob) -> None:
    with pytest.raises(AssertionError, match="outsider content"):
        assert_marker_absent(blob())


def test_marker_probe_reads_events_jsonl(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"event": {"body": OUTSIDER_MARKER}}) + "\n")
    with pytest.raises(AssertionError, match="outsider content"):
        assert_marker_absent(events)
    assert_marker_absent({"outer": ["safe"]}, '[{"content":"safe"}]')


def _assert_github_fetch_cache_closed(root: Path) -> None:
    sources = [root / "mimir/tools/forge.py", root / "mimir/pollers.py"]
    sources.extend((root / "mimir/forge").rglob("*.py"))
    sources.extend((root / "mimir/pollers").rglob("*.py"))
    # Include both shipped GitHub pollers and any future GitHub skill scripts.
    skills = root / "mimir/optional-skills"
    for name in ("github-poller", "github-ci-watch"):
        assert (skills / name / "scripts/poller.py").is_file(), name
    for skill in skills.glob("github-*"):
        sources.extend(skill.rglob("*.py"))
    for source in sources:
        # Also catches component-built paths, not just the slash spelling.
        assert "fetch-cache" not in source.read_text(encoding="utf-8"), source


def test_forge_and_github_pollers_do_not_read_fetch_cache() -> None:
    _assert_github_fetch_cache_closed(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("relative_path", [
    "mimir/tools/forge.py",
    "mimir/forge/nested/client.py",
    "mimir/pollers/nested/github.py",
    "mimir/optional-skills/github-poller/scripts/poller.py",
    "mimir/optional-skills/github-ci-watch/scripts/poller.py",
    "mimir/optional-skills/github-future/scripts/poller.py",
])
@pytest.mark.parametrize("injected_source", [
    '_CACHE = "attachments/fetch-cache"\n',
    '_CACHE = ("attachments", "fetch-cache")\n',
    '_CACHE = home / "attachments" / "fetch-cache"\n',
])
def test_fetch_cache_closure_detects_injected_path_spellings(
    tmp_path: Path, relative_path: str, injected_source: str,
) -> None:
    for required in (
        "mimir/tools/forge.py", "mimir/pollers.py",
        "mimir/optional-skills/github-poller/scripts/poller.py",
        "mimir/optional-skills/github-ci-watch/scripts/poller.py",
    ):
        source = tmp_path / required
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# no cache access\n", encoding="utf-8")
    _assert_github_fetch_cache_closed(tmp_path)
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(injected_source, encoding="utf-8")
    with pytest.raises(AssertionError, match=relative_path):
        _assert_github_fetch_cache_closed(tmp_path)
