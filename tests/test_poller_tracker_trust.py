"""Core ingest trust for ``trust_source: tracker`` pollers.

Tracker items are attested server-side through ``PollerHooks.trust_attestor``;
a fake attestor stands in for any plugin, so core never imports Cultivate.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from mimir import poller_recovery, pollers
from mimir.event_logger import init_logger
from mimir.models import AgentEvent
from mimir.poller_hooks import ItemAttestation, PollerHooks
from mimir.pollers import PollerConfig, discover_pollers, run_poller

URL = "https://github.com/acme/widget/issues/7"
EXTRAS = {
    "delivery_key": "trk-" + "a" * 24,
    "adapter": "github_issues",
    "ref": "acme/widget#7",
    "url": URL,
    "kind": "triage",
}
PROMPT = "Triage this issue"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-tracker-trust")
    return tmp_path


def _events(home: Path) -> list[dict]:
    path = home / "logs" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class _Enqueue:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> bool:
        self.events.append(event)
        return True


def _poller(tmp_path: Path, items: list[dict], *, attestor=None, batch_size: int = 1) -> PollerConfig:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    script = skill_dir / "poller.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport json\n"
        f"for item in {items!r}:\n    print(json.dumps(item))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return PollerConfig(
        name="tracker-poller",
        command=f"{sys.executable} poller.py",
        cron="* * * * *",
        env={},
        skill_dir=skill_dir,
        batch_size=batch_size,
        trust_source="tracker",
        hooks=PollerHooks(trust_attestor=attestor) if attestor else None,
    )


def _item(**updates: object) -> dict:
    item = {"poller": "tracker-poller", "prompt": PROMPT, **EXTRAS,
            "actor": "forged", "author_is_trusted": True, "tracker_attested": True}
    item.update(updates)
    return item


def _trusting(seen: list | None = None):
    def attest(extras, env):
        if seen is not None:
            seen.append((dict(extras), dict(env)))
        return ItemAttestation(trusted=True, actor="alice", prompt=PROMPT)
    return attest


@pytest.mark.asyncio
async def test_attested_item_is_delivered_trusted_with_server_extras(tmp_path, home):
    seen: list = []
    poller = _poller(tmp_path, [_item(secret_claim="x")], attestor=_trusting(seen))
    enq = _Enqueue()

    await run_poller(poller, enqueue=enq)

    [event] = enq.events
    [extras] = event.extra["items"]
    assert extras["actor"] == "alice"
    assert extras["author_is_trusted"] is True
    assert extras["tracker_attested"] is True
    assert "secret_claim" not in extras
    assert set(seen[0][0]) == set(EXTRAS)
    [source] = event.ifc_labels.sources
    assert source.integrity == "trusted"


@pytest.mark.asyncio
async def test_forged_claims_do_not_make_an_unattested_item_trusted(tmp_path, home):
    def attest(extras, env):
        return ItemAttestation(trusted=False)

    poller = _poller(tmp_path, [_item()], attestor=attest)
    enq = _Enqueue()

    await run_poller(poller, enqueue=enq)

    assert enq.events == []
    [withheld] = [e for e in _events(home) if e["type"] == "poller_item_withheld"]
    assert withheld["reason"] == "attestation_refused"
    assert withheld["adapter"] == "github_issues"
    assert withheld["ref"] == "acme/widget#7"
    assert PROMPT not in json.dumps(withheld)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attest", "reason"),
    [
        (lambda e, env: ItemAttestation(trusted=None), "attestation_unavailable"),
        (lambda e, env: (_ for _ in ()).throw(RuntimeError("boom")), "attestation_unavailable"),
        (lambda e, env: ItemAttestation(trusted=True, prompt="different"), "prompt_mismatch"),
        (lambda e, env: ItemAttestation(trusted=True, prompt=None), "prompt_mismatch"),
    ],
)
async def test_unavailable_or_mismatched_attestation_drops_the_item(
    tmp_path, home, attest, reason,
):
    enq = _Enqueue()

    await run_poller(_poller(tmp_path, [_item()], attestor=attest), enqueue=enq)

    assert enq.events == []
    [withheld] = [e for e in _events(home) if e["type"] == "poller_item_withheld"]
    assert withheld["reason"] == reason


@pytest.mark.asyncio
async def test_no_attestor_fails_closed(tmp_path, home):
    enq = _Enqueue()

    await run_poller(_poller(tmp_path, [_item()]), enqueue=enq)

    assert enq.events == []
    [withheld] = [e for e in _events(home) if e["type"] == "poller_item_withheld"]
    assert withheld["reason"] == "attestation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"adapter": "Bad Adapter"},
        {"ref": "acme/widget#7;rm"},
        {"url": "http://github.com/acme/widget/issues/7"},
        {"url": "https://github.com/acme/widget/issues/8"},
        {"delivery_key": "nope"},
        {"kind": "other"},
        {"attested_event_id": "not-an-event"},
    ],
)
async def test_invalid_extras_are_dropped_before_attestation(tmp_path, home, bad):
    seen: list = []
    enq = _Enqueue()

    await run_poller(_poller(tmp_path, [_item(**bad)], attestor=_trusting(seen)), enqueue=enq)

    assert enq.events == [] and seen == []
    [withheld] = [e for e in _events(home) if e["type"] == "poller_item_withheld"]
    assert withheld["reason"] == "invalid_item"


@pytest.mark.asyncio
async def test_multi_item_fire_attests_nothing(tmp_path, home):
    seen: list = []
    other = {**EXTRAS, "ref": "acme/widget#8", "url": "https://github.com/acme/widget/issues/8",
             "delivery_key": "trk-" + "b" * 24}
    items = [_item(), _item(**other)]
    poller = _poller(tmp_path, items, attestor=_trusting(seen), batch_size=2)
    enq = _Enqueue()

    await run_poller(poller, enqueue=enq)

    assert enq.events == [] and seen == []


@pytest.mark.asyncio
async def test_attestation_budget_is_spent_per_fire(tmp_path, home, monkeypatch):
    monkeypatch.setattr(pollers, "TRACKER_ATTESTATIONS_PER_FIRE", 1)
    seen: list = []
    other = {**EXTRAS, "ref": "acme/widget#8", "url": "https://github.com/acme/widget/issues/8",
             "delivery_key": "trk-" + "b" * 24}
    poller = _poller(tmp_path, [_item(), _item(**other)], attestor=_trusting(seen))
    enq = _Enqueue()

    await run_poller(poller, enqueue=enq)

    assert len(seen) == 1 and len(enq.events) == 1
    [withheld] = [e for e in _events(home) if e["type"] == "poller_item_withheld"]
    assert withheld["reason"] == "attestation_unavailable"


def test_tracker_trust_source_without_attestor_or_batch_one_is_reported(tmp_path):
    skills = tmp_path / "skills"
    skill = skills / "tracker-poller"
    skill.mkdir(parents=True)
    (skill / "pollers.json").write_text(json.dumps({"pollers": [{
        "name": "tracker-poller", "command": "true", "cron": "* * * * *",
        "trust_source": "tracker", "batch_size": 3,
    }]}), encoding="utf-8")
    invalid: list = []

    [poller] = discover_pollers(skills, invalid_entries=invalid)

    assert poller.trust_source == "tracker"
    assert [reason for _, _, reason in invalid] == [
        "trust_source 'tracker' requires a trust-attestor hooks profile and "
        "batch_size 1; items fail closed"
    ]


def test_jira_site_is_a_non_secret_forwarded_key():
    assert "JIRA_SITE" in pollers._NON_SECRET_FORWARDED_ENV_KEYS
    assert "JIRA_API_TOKEN" not in pollers._NON_SECRET_FORWARDED_ENV_KEYS


# --- recovery ---------------------------------------------------------


async def _stash_and_reconcile(tmp_path, item, *, attestor, redact="identity"):
    event = AgentEvent(
        trigger="poller", channel_id="poller:tracker-poller", content=PROMPT,
        source_id="poller:tracker-poller:1:batch:0", source="poller",
        extra={"poller_name": "tracker-poller", "items": [item]},
    )
    await poller_recovery.stash_enqueued_event(tmp_path, event, pending_enqueue=True)
    replays: list[AgentEvent] = []

    async def enqueue(e):
        replays.append(e)
        return True

    await poller_recovery.reconcile_failed_turns(
        poller_name="tracker-poller", channel_id=event.channel_id,
        persist_dir=tmp_path, events_path=tmp_path / "events.jsonl", enqueue=enqueue,
        service_principal="poller:tracker-poller", service_authority=object(),
        trust_source="tracker", trust_attestor=attestor,
        attestor_env={"K": "v"},
        attestation_redact=None if redact is None else (lambda text: text),
    )
    return replays


@pytest.mark.asyncio
async def test_recovery_reattests_and_replaces_stashed_claims(tmp_path):
    seen: list = []
    item = {**EXTRAS, "actor": "forged", "author_is_trusted": True, "junk": 1}

    [replayed] = await _stash_and_reconcile(tmp_path, item, attestor=_trusting(seen))

    [extras] = replayed.extra["items"]
    assert extras["actor"] == "alice" and extras["tracker_attested"] is True
    assert "junk" not in extras
    assert set(seen[0][0]) == set(EXTRAS) and seen[0][1] == {"K": "v"}
    [source] = replayed.ifc_labels.sources
    assert source.integrity == "trusted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attest",
    [
        lambda e, env: ItemAttestation(trusted=False, prompt=PROMPT),
        lambda e, env: ItemAttestation(trusted=None),
        lambda e, env: ItemAttestation(trusted=True, prompt="stale"),
        lambda e, env: (_ for _ in ()).throw(RuntimeError("down")),
        None,
    ],
)
async def test_recovery_drops_unattested_tracker_events(tmp_path, attest):
    item = {**EXTRAS, "actor": "forged", "author_is_trusted": True, "tracker_attested": True}

    assert await _stash_and_reconcile(tmp_path, item, attestor=attest) == []


@pytest.mark.asyncio
async def test_recovery_without_redactor_drops(tmp_path):
    assert await _stash_and_reconcile(
        tmp_path, dict(EXTRAS), attestor=_trusting(), redact=None,
    ) == []


@pytest.mark.asyncio
async def test_recovery_requires_exactly_one_item(tmp_path):
    event = AgentEvent(
        trigger="poller", channel_id="poller:tracker-poller", content=PROMPT,
        source_id="poller:tracker-poller:1:batch:0", source="poller",
        extra={"poller_name": "tracker-poller", "items": [dict(EXTRAS), dict(EXTRAS)]},
    )
    await poller_recovery.stash_enqueued_event(tmp_path, event, pending_enqueue=True)
    replays: list = []

    async def enqueue(e):
        replays.append(e)
        return True

    await poller_recovery.reconcile_failed_turns(
        poller_name="tracker-poller", channel_id=event.channel_id,
        persist_dir=tmp_path, events_path=tmp_path / "events.jsonl", enqueue=enqueue,
        service_principal="poller:tracker-poller", service_authority=object(),
        trust_source="tracker", trust_attestor=_trusting(),
        attestor_env={}, attestation_redact=lambda t: t,
    )
    assert replays == []
