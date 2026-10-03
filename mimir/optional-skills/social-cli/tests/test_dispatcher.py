"""Merged-file boundary and once-only dispatch through a fake social-cli."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


POLLERS = ("social-cli-notifications", "social-cli-feed")
POST = "dispatch:\n  - post: {platform: bsky, text: A public update}\n"


def git(home: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=home, check=True, capture_output=True)


@pytest.fixture(params=POLLERS)
def setup(tmp_path: Path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    poller = request.param
    state = home / "state/pollers" / poller
    state.mkdir(parents=True)
    outbox = home / "state/social-outbox" / poller
    outbox.mkdir(parents=True)
    git(home, "init", "-q")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.org")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.org")
    (home / "seed").write_text("base")
    git(home, "add", "seed")
    git(home, "commit", "-qm", "base")
    binary = tmp_path / "social-cli"
    binary.write_text(f"#!{sys.executable}\n"
                      "import json, os, sys\n"
                      "from pathlib import Path\n"
                      "with open(os.environ['CALLS'], 'a') as f:\n"
                      "    snapshot = Path(sys.argv[-1]) if sys.argv[1] == 'dispatch' else None\n"
                      "    f.write(json.dumps({'argv': sys.argv[1:], 'text': snapshot.read_text() if snapshot else None, 'mode': snapshot.stat().st_mode & 0o777 if snapshot else None, 'parent_mode': snapshot.parent.stat().st_mode & 0o777 if snapshot else None, 'ledger': Path(os.environ['LEDGER']).read_text() if Path(os.environ['LEDGER']).exists() else ''}) + '\\n')\n"
                      "if os.environ.get('DRY_FAIL') and '--dry-run' in sys.argv: print('invalid outbox', file=sys.stderr); sys.exit(7)\n"
                      "if os.environ.get('DRY_OUTPUT_FAIL') and '--dry-run' in sys.argv: print('Action 0: No recognized action type'); sys.exit(0)\n"
                      "if os.environ.get('CRASH') and sys.argv[1] == 'dispatch' and '--dry-run' not in sys.argv: print('real failure', file=sys.stderr); sys.exit(7)\n")
    binary.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("LEDGER", str(state / "dispatched-ledger.jsonl"))
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("POLLER_NAME", poller)
    monkeypatch.setenv("SOCIAL_CLI_BIN", str(binary))
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    monkeypatch.setenv("MIMIR_SOCIAL_OUTBOX_APPROVERS", "operator")
    monkeypatch.delenv("MIMIR_GITHUB_SELF_LOGIN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-forge-placeholder")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    module = importlib.import_module("poller" if poller == POLLERS[0] else "feed_poller")
    monkeypatch.setattr(module, "STATE_DIR", state)
    monkeypatch.setattr(module, "CURSOR_FILE", state / "emitted.json")
    monkeypatch.setattr(module, "POLLER_NAME", poller)
    # Fake only the forge transport; production Git/provenance checks still run.
    import mimir.proposals as proposals
    original = proposals._run

    def forge(args, *, cwd, capture):
        if args[:3] != ["gh", "pr", "list"]:
            return original(args, cwd=cwd, capture=capture)
        history = subprocess.run(["git", "log", "--format=%H %s"], cwd=cwd,
                                 check=True, capture_output=True, text=True).stdout
        prs = [{"state": "MERGED", "headRefName": f"poller/{poller}/social-outbox",
                "mergeCommit": {"oid": line.split()[0]}, "mergedBy": {"login": "operator"}}
               for line in history.splitlines() if line.endswith("merge outbox")]
        return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")

    monkeypatch.setattr(proposals, "_run", forge)
    return home, state, outbox, calls, module


def commit_file(home: Path, path: Path, text: str = POST) -> None:
    path.write_text(text)
    git(home, "add", str(path.relative_to(home)))
    git(home, "commit", "-qm", "merge outbox")


def dispatches(calls: Path) -> list[dict]:
    return [record for line in calls.read_text().splitlines()
            if (record := json.loads(line))["argv"][0] == "dispatch" and "--dry-run" not in record["argv"]] if calls.exists() else []


def test_merged_clean_once_and_before_sync(setup):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    assert module.main() == 0
    assert module.main() == 0
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(dispatches(calls)) == 1
    assert records[0]["argv"] == ["dispatch", "--dry-run", records[0]["argv"][-1]]
    assert records[1]["argv"] == ["dispatch", records[0]["argv"][-1]]
    assert "--file" not in records[0]["argv"] + records[1]["argv"]
    assert records[0]["argv"][-1] != str(path)
    assert records[0]["text"] == POST
    assert records[0]["mode"] == 0o600
    assert records[0]["parent_mode"] == 0o700
    assert not Path(records[0]["argv"][-1]).exists()
    assert len(state.joinpath("dispatched-ledger.jsonl").read_text().splitlines()) == 1
    assert records[0]["ledger"] == ""
    assert hashlib.sha256(POST.encode()).hexdigest() in records[1]["ledger"]


@pytest.mark.parametrize("platform", ["bsky", "x"])
@pytest.mark.parametrize("form", ["platform", "list", "mapping", "follow", "ignore"])
def test_snapshot_basename_matches_action_platform(setup, monkeypatch, platform, form):
    import yaml

    home, state, root, calls, module = setup
    # Named actions, including zero-cap follow, must override the legacy default.
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", platform if form == "ignore" else "bsky")
    payload = {"platform": platform, "text": "Hi"}
    if form == "list":
        payload = {"platforms": [platform], "text": "Hi"}
    elif form == "mapping":
        payload = {"platforms": {platform: "Hi"}}
    entry = {"post": payload}
    if form == "follow":
        entry = {"follow": {"platform": platform, "handle": "example"}}
    elif form == "ignore":
        entry = {"ignore": {"id": "notif"}}
    commit_file(home, root / "outbox-arbitrary-name.yaml", yaml.safe_dump({"dispatch": [entry]}))
    assert module.main() == 0
    records = [json.loads(line) for line in calls.read_text().splitlines()
               if json.loads(line)["argv"][0] == "dispatch"]
    assert len(records) == 2
    snapshot = records[0]["argv"][-1]
    assert Path(snapshot).name == f"outbox-{platform}.yaml"
    assert records[0]["argv"] == ["dispatch", "--dry-run", snapshot]
    assert records[1]["argv"] == ["dispatch", snapshot]
    assert records[0]["ledger"] == "" and records[1]["ledger"]


@pytest.mark.parametrize("text", [
    "dispatch:\n  - post: {text: hi, platforms: [bsky, x]}\n",
    "dispatch:\n  - post: {platforms: {bsky: hi, x: hello}}\n",
    "dispatch:\n  - reply: {platform: bsky, id: post, text: hi}\n  - like: {platform: x, id: '123'}\n",
    "dispatch:\n  - follow: {platform: bsky, handle: example}\n  - bookmark: {platform: x, id: '123'}\n",
    "dispatch:\n  - post: {platform: bsky, text: hi, quoteId: q, replyTo: r}\n",
])
def test_invalid_platform_or_post_targets_withheld_before_dry_run_and_ledger(setup, capsys, text):
    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-invalid.yaml", text)
    assert module.main() == 0
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not any(record["argv"][0] == "dispatch" for record in records)
    assert (state / "dispatched-ledger.jsonl").read_text() == ""
    assert any(json.loads(line)["reason"] == "cap_check_refused"
               for line in capsys.readouterr().out.splitlines())


@pytest.mark.parametrize("failure", ["DRY_FAIL", "DRY_OUTPUT_FAIL"])
def test_failed_dry_run_does_not_consume_ledger_or_cap(setup, monkeypatch, capsys, failure):
    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    monkeypatch.setenv(failure, "1")
    module.main()
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len([r for r in records if r["argv"][0] == "dispatch"]) == 1
    assert records[0]["argv"][1] == "--dry-run" and records[0]["ledger"] == ""
    assert (state / "dispatched-ledger.jsonl").read_text() == ""
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert signals[0]["reason"] == "dry_run_failed" and "stderr" in signals[0]
    monkeypatch.delenv(failure)
    module.main()
    assert len(dispatches(calls)) == 1
    assert json.loads((state / "dispatched-ledger.jsonl").read_text())["posts"] == {"bsky": 1}


@pytest.mark.parametrize("text,expected", [
    ("dispatch:\n  - post: {text: hi, platforms: [x]}\n", {"x": 1}),
    ("dispatch:\n  - post: {platforms: {bsky: hi}}\n", {"bsky": 1}),
    ("dispatch:\n  - thread: {platform: bsky, posts: [one, two, three]}\n", {"bsky": 3}),
    ("dispatch:\n  - like: {platform: bsky, id: 'at://post'}\n"
     "  - ignore: {id: notif_003}\n"
     "  - annotate: {platform: bsky, id: 'https://example.org', text: hi}\n"
     "  - follow: {platform: bsky, handle: example.bsky.social}\n"
     "  - bookmark: {platform: bsky, id: '123'}\n"
     "  - highlight: {platform: bsky, id: 'at://post', quote: Excerpt}\n", {}),
    ("processed: [notif_003]\ndispatch:\n"
     "  - reply: {platform: bsky, id: 'at://post', text: hi, notificationId: notif_003, idempotencyKey: r1}\n"
     "  - post: {text: hi, platforms: [bsky], quoteId: 'at://quote', idempotencyKey: p1}\n"
     "  - thread: {platform: bsky, posts: [one, two], replyTo: '123', idempotencyKey: t1}\n",
     {"bsky": 4}),
])
def test_cap_units_from_validated_schema(setup, text, expected):
    import dispatcher

    _, state, _, _, _ = setup
    assert dispatcher._cap_allows(text, state, {}) == expected


@pytest.mark.parametrize("action,payload", [
    ("post", {"platform": "bsky", "text": "hi"}),
    ("follow", {"platform": "bsky", "handle": "example.bsky.social"}),
])
def test_media_refused_before_dry_run_and_ledger(setup, capsys, action, payload):
    import yaml

    home, state, root, calls, module = setup
    text = yaml.safe_dump({"dispatch": [{action: {**payload, "media": ["local-image.png"]}}]})
    commit_file(home, root / "outbox-media.yaml", text)
    assert module.main() == 0
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert not any(record["argv"][0] == "dispatch" for record in records)
    assert (state / "dispatched-ledger.jsonl").read_text() == ""
    assert any(json.loads(line)["reason"] == "cap_check_refused"
               for line in capsys.readouterr().out.splitlines())


def test_dispatch_revalidates_shared_schema(setup, monkeypatch):
    import mimir.social_outbox as schema
    import dispatcher

    _, state, _, _, _ = setup
    monkeypatch.setattr(schema, "validate_outbox", lambda doc: ["forced rejection"])
    assert dispatcher._cap_allows(POST, state, {}) is None


def test_stray_outbox_name_is_flagged_once_per_content_and_never_dispatched(setup, capsys):
    home, state, root, calls, module = setup
    stray = root / "outbox-2026-10-02-ship-hack-engineering.md"
    commit_file(home, stray)
    original = stray.read_bytes()
    for expected in (1, 0):
        assert module.main() == 0
        signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert signals == ([{"poller": module.POLLER_NAME,
                             "signal": "social_outbox_dispatch_withheld",
                             "reason": "unrecognized_outbox_name",
                             "path": stray.relative_to(home).as_posix()}] if expected else [])
        assert not dispatches(calls)
        assert stray.read_bytes() == original
    stray.write_text(POST.replace("public", "changed"))
    assert module.main() == 0
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(signals) == 1 and signals[0]["reason"] == "unrecognized_outbox_name"
    assert not dispatches(calls)
    assert stray.read_text() == POST.replace("public", "changed")
    records = [json.loads(line) for line in (state / "unrecognized-outbox-ledger.jsonl").read_text().splitlines()]
    assert [r["path"] for r in records] == [stray.relative_to(home).as_posix()] * 2
    assert records[0]["sha256"] != records[1]["sha256"]


def test_approver_merged_stray_is_excluded_before_dispatch_candidates(setup, monkeypatch, capsys):
    import dispatcher
    from mimir.proposals import merged_social_outbox_commit

    home, state, root, calls, module = setup
    stray = root / "outbox-2026-10-02-ship-hack-engineering.md"
    commit_file(home, stray)
    original = stray.read_bytes()
    rel = stray.relative_to(home).as_posix()
    merge_oid = subprocess.run(["git", "rev-parse", "HEAD"], cwd=home,
                               check=True, capture_output=True, text=True).stdout.strip()
    # commit_file's "merge outbox" subject is translated by setup's fake forge
    # into a MERGED rolling PR whose merger is the configured approver. Prove
    # the real provenance gate passes, rather than relying on its refusal.
    withheld = []
    assert merged_social_outbox_commit(
        home, module.POLLER_NAME, rel, verified_text=POST,
        on_withheld=withheld.append,
    ) == merge_oid
    assert withheld == []

    valid = root / "outbox-approved.yaml"
    valid_text = POST.replace("public", "approved YAML")
    commit_file(home, valid, valid_text)
    assert merged_social_outbox_commit(
        home, module.POLLER_NAME, rel, verified_text=POST,
    ) == merge_oid

    # The regex guard is deliberately redundant with the narrow glob. Pin
    # candidate enumeration independently so widening only the glob fails,
    # even though the second guard would still prevent an actual dispatch.
    original_glob = Path.glob
    candidates = []

    def observe_glob(path, pattern, *args, **kwargs):
        for candidate in original_glob(path, pattern, *args, **kwargs):
            if path == root:
                candidates.append(candidate)
            yield candidate

    monkeypatch.setattr(dispatcher.Path, "glob", observe_glob)
    for expected_signals in (1, 0):
        assert module.main() == 0
        signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert signals == ([{"poller": module.POLLER_NAME,
                             "signal": "social_outbox_dispatch_withheld",
                             "reason": "unrecognized_outbox_name", "path": rel}]
                           if expected_signals else [])
        assert candidates == [valid]
        candidates.clear()
        dispatched = dispatches(calls)
        assert len(dispatched) == 1
        assert dispatched[0]["text"] == valid_text
        assert stray.read_bytes() == original
    flags = [json.loads(line) for line in
             (state / "unrecognized-outbox-ledger.jsonl").read_text().splitlines()]
    assert flags == [{"path": rel, "sha256": hashlib.sha256(original).hexdigest()}]
    sent = [json.loads(line) for line in
            (state / "dispatched-ledger.jsonl").read_text().splitlines()]
    assert [entry["path"] for entry in sent] == [valid.relative_to(home).as_posix()]


@pytest.mark.parametrize("case", ["untracked", "dirty", "staged"])
def test_unmerged_files_never_dispatch(setup, case, capsys):
    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    if case in {"dirty", "staged"}:
        commit_file(home, path)
        path.write_text(POST.replace("public", "changed"))
        if case == "staged":
            git(home, "add", str(path.relative_to(home)))
            path.write_text(POST)
    else:
        path.write_text(POST)
    module.main()
    assert dispatches(calls) == []
    assert "unmerged or dirty" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["privacy", "cap"])
def test_scan_and_cap_refuse(setup, case, capsys):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    if case == "privacy":
        commit_file(home, path, POST.replace("A public update", "ghp_" + "A" * 36))
    else:
        commit_file(home, path)
        from datetime import datetime, timezone
        import yaml
        now = datetime.now(timezone.utc).isoformat()
        (state / "sent_ledger-bsky.yaml").write_text(yaml.safe_dump([
            {"action": "post", "platform": "bsky", "timestamp": now, "createdId": str(i)}
            for i in range(5)
        ]))
    module.main()
    assert not dispatches(calls)
    assert ("privacy scan refused" if case == "privacy" else "cap check refused") in capsys.readouterr().err


def test_crash_after_ledger_write_cannot_retry(setup, monkeypatch, capsys):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    monkeypatch.setenv("CRASH", "1")
    module.main()
    assert "dispatch failed" in capsys.readouterr().err
    monkeypatch.delenv("CRASH")
    module.main()
    assert len(dispatches(calls)) == 1
    assert hashlib.sha256(POST.encode()).hexdigest() in dispatches(calls)[0]["ledger"]


def test_cap_reserves_attempted_posts_across_files(setup, capsys):
    home, state, root, calls, module = setup
    for index in range(6):
        commit_file(home, root / f"outbox-{index}.yaml", POST.replace("public", f"public {index}"))
    module.main()
    assert len(dispatches(calls)) == 5
    assert "cap check refused" in capsys.readouterr().err
    module.main()
    assert len(dispatches(calls)) == 5


def test_corrupt_dispatch_ledger_blocks_all_dispatch(setup, capsys):
    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    (state / "dispatched-ledger.jsonl").write_text('{"sha256":"bad"}\n')
    module.main()
    assert not dispatches(calls)
    assert "invalid dispatched ledger" in capsys.readouterr().err


def test_cap_check_divergence_blocks_even_with_numeric_headroom(setup, capsys):
    from datetime import datetime, timezone

    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    archives = state / "outbox_archive"
    archives.mkdir()
    prefix = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    (archives / f"{prefix}-000Z_outbox-bsky.yaml").write_text(
        "dispatch:\n  - post: {text: first}\n  - post: {text: second}\n")
    module.main()
    assert not dispatches(calls)
    assert "cap check refused" in capsys.readouterr().err


def test_live_autocommitted_file_is_not_merge_approval(setup, capsys):
    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    path.write_text(POST)
    git(home, "add", "-A")
    git(home, "commit", "-qm", "turn auto-commit (not reviewed)")
    module.main()
    assert dispatches(calls) == []
    assert "forge merge approval unavailable" in capsys.readouterr().err


def test_autocommit_after_approved_merge_invalidates_approval(setup, capsys):
    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    path.write_text(POST.replace("public", "unreviewed"))
    git(home, "add", "-A")
    git(home, "commit", "-qm", "turn changed outbox")
    module.main()
    assert dispatches(calls) == []
    assert "forge merge approval unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("case,reason", [
    ("unavailable", "forge_unreachable"), ("nonzero", "forge_unreachable"),
    ("malformed", "malformed_forge_response"),
    ("open", "no_qualifying_merged_pr"), ("wrong-branch", "no_qualifying_merged_pr"),
    ("wrong-commit", "no_qualifying_merged_pr"), ("unlisted-merger", "merger_not_approved"),
    ("missing-merger", "malformed_forge_response"),
])
def test_forge_evidence_fails_closed(setup, monkeypatch, case, reason, capsys):
    import mimir.proposals as proposals

    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    original = proposals._run

    def forge(args, *, cwd, capture):
        result = original(args, cwd=cwd, capture=capture)
        if args[:3] != ["gh", "pr", "list"]:
            return result
        if case == "unavailable":
            raise OSError("offline")
        if case == "nonzero":
            # Even plausible stdout on a failed gh invocation is not approval.
            return subprocess.CompletedProcess(args, 1, result.stdout, "auth unavailable")
        if case == "malformed":
            return subprocess.CompletedProcess(args, 0, "not json", "")
        prs = json.loads(result.stdout)
        if case == "unlisted-merger":
            prs[0]["mergedBy"] = {"login": "UnLiStEd"}
        elif case == "missing-merger":
            prs[0].pop("mergedBy")
        elif case == "open":
            prs[0]["state"] = "OPEN"
        elif case == "wrong-branch":
            prs[0]["headRefName"] = "poller/other/social-outbox"
        elif case == "wrong-commit":
            prs[0]["mergeCommit"]["oid"] = "0" * 40
        return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")

    monkeypatch.setattr(proposals, "_run", forge)
    assert module.main() == 0
    assert dispatches(calls) == []
    output = capsys.readouterr()
    assert "forge merge approval unavailable" in output.err
    signals = [json.loads(line) for line in output.out.splitlines()]
    assert signals == [{"poller": module.POLLER_NAME,
                        "signal": "social_outbox_dispatch_withheld", "reason": reason,
                        "path": (root / "outbox-one.yaml").relative_to(home).as_posix()}]


@pytest.mark.parametrize("tracked_target", [False, True])
def test_symlink_outbox_skipped_before_eligibility_checks(setup, monkeypatch, tracked_target):
    import dispatcher

    home, _, root, calls, module = setup
    target = home / "elsewhere.yaml"
    if tracked_target:
        commit_file(home, target)
    else:
        target.write_text(POST)
    path = root / "outbox-link.yaml"
    path.symlink_to(target)
    original = dispatcher._run
    checked = []

    def observe(args, cwd):
        if args[0] == "git" and path.relative_to(home).as_posix() in args:
            checked.append(args)
        return original(args, cwd)

    monkeypatch.setattr(dispatcher, "_run", observe)
    module.main()
    assert dispatches(calls) == []
    # Pin the early symlink refusal independently of the tracked/mode gate:
    # removing is_symlink() must fail this assertion, not pass incidentally.
    assert checked == []


def test_real_manifest_has_no_agent_dispatch_capability():
    path = Path(__file__).resolve().parents[1] / "pollers.json"
    manifest = json.loads(path.read_text())
    for poller in manifest["pollers"]:
        authority = poller["authority"]
        assert authority["proposal_surface"] == "social-outbox"
        assert {"open_proposal", "submit_proposal", "abandon_proposal"} <= set(authority["capabilities"])
        assert not {"shell_exec", "bash_jobs_list", "bash_job_output"} & set(authority["capabilities"])
        assert "shell_commands" not in authority
        assert {"GITHUB_TOKEN", "GH_TOKEN", "MIMIR_SOCIAL_OUTBOX_APPROVERS", "MIMIR_SOURCE_DIR"} <= set(poller["pass_env"])


@pytest.mark.parametrize("missing,reason", [
    ("GITHUB_TOKEN", "missing_forge_token"),
    ("MIMIR_SOCIAL_OUTBOX_APPROVERS", "no_approvers_configured"),
])
def test_missing_forge_configuration_withholds_with_signal(setup, monkeypatch, capsys, missing, reason):
    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    monkeypatch.delenv(missing)
    assert module.main() == 0
    assert not dispatches(calls)
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s["reason"] for s in signals] == [reason]
    assert signals[0]["signal"] == "social_outbox_dispatch_withheld"


@pytest.mark.parametrize("configured", ["", "   ", " , , "])
def test_empty_approver_list_never_dispatches(setup, monkeypatch, capsys, configured):
    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    monkeypatch.setenv("MIMIR_SOCIAL_OUTBOX_APPROVERS", configured)
    assert module.main() == 0
    assert not dispatches(calls)
    # Notification/feed polling must continue even while dispatch is disabled.
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(r["argv"][0] == ("sync" if module.POLLER_NAME == POLLERS[0] else "feed") for r in records)
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s["reason"] for s in signals] == ["no_approvers_configured"]
    assert signals[0]["signal"] == "social_outbox_dispatch_withheld"


@pytest.mark.parametrize("configured", ["operator,bad login", "*", "operator,@other"])
def test_malformed_approver_list_fails_closed(setup, monkeypatch, capsys, configured):
    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    monkeypatch.setenv("MIMIR_SOCIAL_OUTBOX_APPROVERS", configured)
    assert module.main() == 0
    assert not dispatches(calls)
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s["reason"] for s in signals] == ["invalid_approvers_configured"]


@pytest.mark.parametrize("self_login", [None, "operator", "different-agent"])
def test_allowlisted_merger_does_not_require_separate_agent_identity(setup, monkeypatch, self_login):
    import mimir.proposals as proposals

    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    monkeypatch.setenv("MIMIR_SOCIAL_OUTBOX_APPROVERS", " other, OpErAtOr,other ")
    if self_login is None:
        monkeypatch.delenv("MIMIR_GITHUB_SELF_LOGIN", raising=False)
    else:
        monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", self_login)
    original = proposals._run
    forge_queries = []

    def forge(args, *, cwd, capture):
        if args[0] == "gh":
            forge_queries.append(args)
            # No credential identity lookup: the forge merger allowlist is the gate.
            assert args[:3] == ["gh", "pr", "list"]
        return original(args, cwd=cwd, capture=capture)

    monkeypatch.setattr(proposals, "_run", forge)
    assert module.main() == 0
    assert len(dispatches(calls)) == 1
    assert len(forge_queries) == 1


@pytest.mark.parametrize("case", ["blob", "read-snapshot"])
def test_content_mismatch_is_signaled_and_not_dispatched(setup, monkeypatch, capsys, case):
    import mimir.proposals as proposals

    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    if case == "blob":
        original = proposals._blob_oid
        monkeypatch.setattr(proposals, "_blob_oid", lambda home, ref, rel:
                            "different" if ref == "HEAD" else original(home, ref, rel))
    else:
        original_read = Path.read_bytes
        monkeypatch.setattr(Path, "read_bytes", lambda target:
                            POST.replace("public", "unverified").encode() if target == path
                            else original_read(target))
    assert module.main() == 0
    assert not dispatches(calls)
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s["reason"] for s in signals] == ["content_changed_after_merge"]
    assert signals[0]["signal"] == "social_outbox_dispatch_withheld"


def test_dispatch_uses_verified_snapshot_when_live_file_changes(setup, monkeypatch):
    import dispatcher

    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    original = dispatcher._cap_allows

    def change_live(text, state, reserved):
        allowed = original(text, state, reserved)
        path.write_text(POST.replace("public", "changed after verification"))
        return allowed

    monkeypatch.setattr(dispatcher, "_cap_allows", change_live)
    assert module.main() == 0
    assert dispatches(calls)[0]["text"] == POST
    assert path.read_text() != POST


@pytest.mark.parametrize("missing", ["mimir", "yaml"])
def test_mimir_import_failure_withholds_only_dispatch(setup, monkeypatch, capsys, missing):
    import builtins

    home, _, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    original = builtins.__import__

    def no_mimir(name, *args, **kwargs):
        if name == missing or name.startswith(missing + "."):
            raise ModuleNotFoundError(missing + " unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mimir)
    # Module-top dispatcher imports must remain independent of mimir, too.
    importlib.reload(importlib.import_module("dispatcher"))
    importlib.reload(module)
    # Notification parsing already requires yaml; startup/sync must still run.
    expected = 3 if missing == "yaml" and module.POLLER_NAME == POLLERS[0] else 0
    assert module.main() == expected
    assert not dispatches(calls)
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(r["argv"][0] == ("sync" if module.POLLER_NAME == POLLERS[0] else "feed") for r in records)
    signals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert signals == [{"poller": module.POLLER_NAME, "signal": "social_outbox_dispatch_withheld",
                        "reason": "mimir_import_failure", "path": None}]
