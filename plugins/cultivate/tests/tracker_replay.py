"""Replay harness and contract scenarios shared by the tracker adapter tests.

Fixtures live at ``fixtures/tracker/<adapter>/<scenario>.json``:
``{"source": "<provider API reference + endpoint>", "exchanges": [{"request":
{"method", "url", "json"?}, "response": {"status", "json"?}}]}``, hand-curated from
the provider's documented schemas.

This module is shared by three adapter test files that are written in parallel;
change it only together with all of them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest

from mimir.poller_tracker import safe_url
from mimir_cultivate.tracker import transport as tracker_transport
from mimir_cultivate.tracker.adapters.base import ActionResult, TrackerAdapter
from mimir_cultivate.tracker.attest import UNAVAILABLE, attestation_for
from mimir_cultivate.tracker.thread import Decision, delivery_decision
from mimir_cultivate.tracker.transport import HttpResponse, TrackerUnavailable

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "tracker"
ADAPTERS = ("github_issues", "linear", "jira")
_NO_EVENT = "ev" + "0" * 24


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _normalize_body(body: Any) -> Any:
    if isinstance(body, Mapping) and isinstance(body.get("query"), str):
        return {**body, "query": _collapse(body["query"])}
    return body


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json: Any


class ReplayTransport:
    """A ``TrackerTransport`` that answers from recorded exchanges, strictly in order.

    Method, URL and JSON body must match the next exchange; a GraphQL ``query`` is
    compared with whitespace collapsed and its ``variables`` exactly. Headers are
    not recorded in fixtures; ``assert_authorized`` checks only that each request
    carried an ``Authorization`` header. Unexpected requests are remembered, so
    ``finish`` fails even when an adapter swallows the assertion raised at the call.
    """

    def __init__(self, exchanges: Sequence[Mapping[str, Any]]) -> None:
        self._exchanges = list(exchanges)
        self._next = 0
        self.requests: list[RecordedRequest] = []
        self.failures: list[str] = []

    @classmethod
    def from_fixture(cls, adapter: str, scenario: str, root: Path = FIXTURE_ROOT) -> "ReplayTransport":
        return cls(load_fixture(adapter, scenario, root)["exchanges"])

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any = None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(RecordedRequest(method, url, dict(headers), json))
        if self._next >= len(self._exchanges):
            return self._fail(f"unexpected request #{self._next + 1}: {method} {url}")
        recorded = self._exchanges[self._next]["request"]
        index = self._next + 1
        if method != recorded["method"]:
            return self._fail(f"request #{index} method {method} != {recorded['method']}")
        if url != recorded["url"]:
            return self._fail(f"request #{index} url {url} != {recorded['url']}")
        if _normalize_body(json) != _normalize_body(recorded.get("json")):
            return self._fail(f"request #{index} json body differs from the fixture")
        self._next += 1
        response = self._exchanges[self._next - 1]["response"]
        return HttpResponse(response["status"], response.get("json"))

    def _fail(self, message: str) -> HttpResponse:
        self.failures.append(message)
        raise AssertionError(message)

    def assert_authorized(self) -> None:
        missing = [r.url for r in self.requests if not r.headers.get("Authorization")]
        assert not missing, f"requests without Authorization: {missing}"

    def finish(self) -> None:
        """Fail on any unexpected request or unconsumed exchange."""

        assert not self.failures, self.failures
        assert self._next == len(self._exchanges), (
            f"{len(self._exchanges) - self._next} recorded exchanges were never requested"
        )


def load_fixture(adapter: str, scenario: str, root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    data = json.loads((root / adapter / f"{scenario}.json").read_text(encoding="utf-8"))
    assert isinstance(data.get("source"), str) and data["source"].strip(), "fixture needs a source"
    assert isinstance(data.get("exchanges"), list), "fixture needs exchanges"
    for exchange in data["exchanges"]:
        assert {"method", "url"} <= set(exchange["request"])
        assert isinstance(exchange["response"]["status"], int)
    return data


@pytest.fixture
def forbid_default_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt in: any code that builds the production transport fails the test.

    Code under test must call ``transport.default_transport()`` through the module
    so this patch reaches it.
    """

    def forbidden() -> Any:
        raise AssertionError("the default tracker transport was built in a test")

    monkeypatch.setattr(tracker_transport, "default_transport", forbidden)
    yield


@dataclass(frozen=True)
class ContractScenario:
    """One recorded scenario every listed adapter must satisfy.

    ``expect`` is the outcome common to the listed adapters; ``expect_by_adapter``
    overrides it for one adapter. ``merged_mapping`` names adapters whose test must
    configure a ``merged`` transition for the scenario to be meaningful.
    """

    name: str
    operation: Literal[
        "list", "attest", "comment", "transition", "close", "list_then_read"
    ]
    expect: Mapping[str, Any]
    adapters: tuple[str, ...] = ADAPTERS
    expect_by_adapter: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    merged_mapping: tuple[str, ...] = ()

    def expectation(self, adapter: str) -> Mapping[str, Any]:
        return {**self.expect, **self.expect_by_adapter.get(adapter, {})}


def _attest(name: str, decision: str, reason: str | None, trusted: bool | None, **kw: Any) -> ContractScenario:
    return ContractScenario(
        name, "attest", {"decision": decision, "reason": reason, "trusted": trusted}, **kw
    )


_WITHHELD = ("withhold", "untrusted_latest_activity", False)
_GITHUB_STATE = {"github_issues": {"status": "unsupported", "reason": "", "requests": 0}}

CONTRACT_SCENARIOS: tuple[ContractScenario, ...] = (
    ContractScenario("list_assigned", "list", {"complete": True}),
    ContractScenario("list_capped_incomplete", "list", {"complete": False}),
    _attest("attest_trusted", "deliver", None, True),
    _attest("attest_untrusted_latest", *_WITHHELD),
    _attest("attest_untrusted_title_edit", *_WITHHELD),
    _attest("attest_untrusted_body_edit_masked", *_WITHHELD),
    _attest("attest_untrusted_comment_edit_masked", *_WITHHELD),
    _attest("attest_history_truncated", "withhold", "thread_too_long", False),
    _attest("attest_unavailable", "unavailable", None, None),
    ContractScenario("comment", "comment", {"status": "posted"}),
    ContractScenario(
        "state_applied", "transition", {"status": "applied"}, expect_by_adapter=_GITHUB_STATE
    ),
    ContractScenario(
        "state_not_found",
        "transition",
        {"status": "unavailable", "reason": "state_not_found"},
        expect_by_adapter=_GITHUB_STATE,
    ),
    ContractScenario(
        "state_permission_denied",
        "transition",
        {"status": "unavailable", "reason": "permission_denied"},
        expect_by_adapter=_GITHUB_STATE,
    ),
    ContractScenario(
        "close_default",
        "close",
        {"status": "applied"},
        expect_by_adapter={
            "jira": {"status": "unavailable", "reason": "merged_state_required", "requests": 0}
        },
    ),
    ContractScenario("close_already", "close", {"status": "already"}, merged_mapping=("jira",)),
    ContractScenario(
        "close_configured", "close", {"status": "applied"}, adapters=("jira",),
        merged_mapping=("jira",),
    ),
    _attest(
        "attest_issue_edits_truncated", "withhold", "thread_too_long", False,
        adapters=("github_issues",),
    ),
    _attest(
        "attest_comment_edits_truncated", "withhold", "thread_too_long", False,
        adapters=("github_issues",),
    ),
    _attest(
        "attest_edited_without_history", "withhold", "thread_too_long", False,
        adapters=("github_issues",),
    ),
    _attest("attest_comment_edited_by_untrusted_update_author", *_WITHHELD, adapters=("jira",)),
    _attest("attest_trusted_comment_edited_after_cutoff", *_WITHHELD, adapters=("jira",)),
    _attest("attest_comment_edited_before_cutoff", "deliver", None, True, adapters=("jira",)),
    _attest("attest_trust_lookup_forbidden", "unavailable", None, None, adapters=("jira",)),
    ContractScenario("list_slugged_url", "list", {"complete": True}, adapters=("linear",)),
    ContractScenario(
        "linear_url_mismatch",
        "list_then_read",
        {"complete": False, "read": "malformed_payload"},
        adapters=("linear",),
    ),
)


def scenarios_for(adapter: str) -> tuple[ContractScenario, ...]:
    return tuple(s for s in CONTRACT_SCENARIOS if adapter in s.adapters)


def expected_inventory(adapter: str, extra: Sequence[str] = ()) -> frozenset[str]:
    return frozenset(s.name for s in scenarios_for(adapter)) | frozenset(extra)


def fixture_inventory(adapter: str, root: Path = FIXTURE_ROOT) -> frozenset[str]:
    return frozenset(p.stem for p in (root / adapter).glob("*.json"))


def assert_fixture_inventory(
    adapter: str, extra: Sequence[str] = (), root: Path = FIXTURE_ROOT
) -> None:
    """The fixture files on disk equal the scenario table, plus the adapter's own ``extra``."""

    assert fixture_inventory(adapter, root) == expected_inventory(adapter, extra)


@dataclass(frozen=True)
class ContractOutcome:
    """What an adapter produced for a scenario, in the terms ``expect`` uses."""

    values: Mapping[str, Any]


def run_contract(
    scenario: ContractScenario,
    adapter_name: str,
    build: Callable[[ReplayTransport], TrackerAdapter],
    ref: str,
    *,
    state: str = "Done",
    root: Path = FIXTURE_ROOT,
) -> ContractOutcome:
    """Replay ``scenario`` against the adapter built over its fixture.

    ``build`` receives the ``ReplayTransport`` and returns the adapter. ``ref`` is
    the issue the scenario acts on; ``state`` is the workflow state name for
    ``transition`` scenarios. Replay mismatches and leftover exchanges fail here.
    """

    assert adapter_name in scenario.adapters
    transport = ReplayTransport.from_fixture(adapter_name, scenario.name, root)
    adapter = build(transport)
    values: dict[str, Any] = {}
    if scenario.operation in {"list", "list_then_read"}:
        issues, complete = adapter.list_assigned()
        values["complete"] = complete
        for issue in issues:
            assert safe_url(issue.url, issue.ref) == issue.url, "listed url is not canonical"
        if scenario.operation == "list_then_read":
            try:
                adapter.read(ref)
            except TrackerUnavailable as exc:
                values["read"] = exc.code
            else:
                values["read"] = "returned"
    elif scenario.operation == "attest":
        try:
            issue = adapter.read(ref)
            decision = delivery_decision(issue, adapter.trust)
        except TrackerUnavailable:
            decision = Decision("unavailable")
            verdict = UNAVAILABLE
        else:
            cutoff_id = decision.cutoff.id if decision.cutoff else _NO_EVENT
            verdict = attestation_for(issue, decision, "triage", cutoff_id)
        values.update(decision=decision.kind, reason=decision.reason, trusted=verdict.trusted)
        values["attestation"] = verdict
    else:
        action: ActionResult
        if scenario.operation == "comment":
            action = adapter.comment(ref, "Thanks, filed.")
        elif scenario.operation == "transition":
            action = adapter.transition(ref, state)
        else:
            action = adapter.close(ref)
        values.update(status=action.status, reason=action.reason)
    values["requests"] = len(transport.requests)
    transport.assert_authorized()
    transport.finish()
    return ContractOutcome(values)


def assert_outcome(scenario: ContractScenario, adapter_name: str, outcome: ContractOutcome) -> None:
    """Compare the outcome with the scenario's expectation for ``adapter_name``."""

    for key, expected in scenario.expectation(adapter_name).items():
        assert outcome.values.get(key) == expected, (scenario.name, adapter_name, key)
