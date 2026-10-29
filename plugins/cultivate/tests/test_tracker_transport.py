from __future__ import annotations

import http.client
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from datetime import UTC, datetime

from mimir_cultivate.tracker import transport
from mimir_cultivate.tracker.adapters.base import ActionResult
from mimir_cultivate.tracker.thread import Activity, TrackerIssue
from mimir_cultivate.tracker.transport import (
    HttpResponse,
    TrackerUnavailable,
    UrllibTransport,
    default_transport,
    unavailable_for_status,
)
from tracker_replay import (
    ADAPTERS,
    CONTRACT_SCENARIOS,
    ReplayTransport,
    assert_fixture_inventory,
    assert_outcome,
    run_contract,
    expected_inventory,
    fixture_inventory,
    forbid_default_transport,  # noqa: F401  (opt-in fixture)
    load_fixture,
    scenarios_for,
)

URL = "https://example.test/api"


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float) -> Any:
        self.requests.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch):
    def install(outcome: Any) -> FakeOpener:
        fake = FakeOpener(outcome)
        monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *handlers: fake)
        return fake

    return install


def call(**kwargs: Any) -> HttpResponse:
    return UrllibTransport().request("POST", URL, headers={"Authorization": "x"}, timeout=3, **kwargs)


def test_success_parses_json_and_sends_body(opener) -> None:
    fake = opener(FakeResponse(200, b'{"ok": true}'))

    response = call(json={"a": 1})

    assert response == HttpResponse(200, {"ok": True})
    request, timeout = fake.requests[0]
    assert request.data == b'{"a": 1}' and timeout == 3
    assert request.get_header("Content-type") == "application/json"
    assert request.get_method() == "POST"


def test_empty_body_is_none(opener) -> None:
    opener(FakeResponse(204, b""))

    assert call() == HttpResponse(204, None)


def test_unparseable_success_body_is_malformed(opener) -> None:
    opener(FakeResponse(200, b"<html>"))

    with pytest.raises(TrackerUnavailable) as caught:
        call()

    assert caught.value.code == "malformed_payload"


def test_error_status_is_returned_not_raised(opener) -> None:
    error = urllib.error.HTTPError(URL, 403, "no", {}, io.BytesIO(b'{"message": "denied"}'))
    opener(error)

    assert call() == HttpResponse(403, {"message": "denied"})


def test_error_status_with_non_json_body_keeps_the_status(opener) -> None:
    opener(urllib.error.HTTPError(URL, 502, "bad", {}, io.BytesIO(b"<html>")))

    assert call() == HttpResponse(502, None)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (TimeoutError("secret text"), "timeout"),
        (urllib.error.URLError(TimeoutError("secret text")), "timeout"),
        (urllib.error.URLError("secret text"), "transport"),
        (ConnectionResetError("secret text"), "transport"),
        (http.client.BadStatusLine("secret text"), "transport"),
    ],
)
def test_network_failures_carry_only_a_code(opener, failure: BaseException, code: str) -> None:
    opener(failure)

    with pytest.raises(TrackerUnavailable) as caught:
        call()

    assert caught.value.code == code
    assert "secret" not in str(caught.value) and caught.value.__suppress_context__ is True


class BrokenBody:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        raise http.client.IncompleteRead(b"secret text")

    def __enter__(self) -> "BrokenBody":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None


def test_incomplete_success_body_carries_only_a_code(opener) -> None:
    opener(BrokenBody())

    with pytest.raises(TrackerUnavailable) as caught:
        call()

    assert caught.value.code == "transport"
    assert "secret" not in str(caught.value) and caught.value.__suppress_context__ is True


def test_unreadable_error_body_carries_only_a_code(opener) -> None:
    opener(urllib.error.HTTPError(URL, 502, "bad", {}, BrokenBody()))  # type: ignore[arg-type]

    with pytest.raises(TrackerUnavailable) as caught:
        call()

    assert caught.value.code == "transport"
    assert "secret" not in str(caught.value) and caught.value.__suppress_context__ is True


def test_plain_http_is_refused_without_a_request(opener) -> None:
    fake = opener(FakeResponse(200, b"{}"))

    with pytest.raises(TrackerUnavailable) as caught:
        UrllibTransport().request("GET", "http://example.test/", headers={}, timeout=1)

    assert caught.value.code == "transport" and fake.requests == []


def test_redirects_are_not_followed() -> None:
    handler = transport._NoRedirect()

    assert handler.redirect_request(object(), object(), 302, "Found", {}, "https://evil/") is None


def test_opener_refuses_to_follow_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Redirecting(http.client.HTTPSConnection):
        def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> None:
            seen.append(url)

        def getresponse(self) -> Any:
            raw = b"HTTP/1.1 302 Found\r\nLocation: https://evil.test/next\r\nContent-Length: 0\r\n\r\n"

            class Sock:
                def makefile(self, *args: Any, **kwargs: Any) -> io.BytesIO:
                    return io.BytesIO(raw)

            response = http.client.HTTPResponse(Sock())  # type: ignore[arg-type]
            response.begin()
            return response

        def connect(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", lambda self, req: self.do_open(Redirecting, req))

    assert call() == HttpResponse(302, None)
    assert len(seen) == 1


def test_default_transport_is_urllib() -> None:
    assert isinstance(default_transport(), UrllibTransport)


@pytest.mark.parametrize("code", ["transport", "timeout", "rate_limited", "http_503", "malformed_payload"])
def test_unavailable_accepts_the_documented_codes(code: str) -> None:
    assert TrackerUnavailable(code).code == code


@pytest.mark.parametrize("code", ["boom", "http_99", "http_5xx", "Rate limited: body text", ""])
def test_unavailable_rejects_free_text(code: str) -> None:
    with pytest.raises(ValueError):
        TrackerUnavailable(code)


def test_status_maps_to_codes() -> None:
    assert unavailable_for_status(429).code == "rate_limited"
    assert unavailable_for_status(503).code == "http_503"
    assert unavailable_for_status(0).code == "transport"


def test_forbid_default_transport_fixture_blocks_construction(forbid_default_transport) -> None:
    with pytest.raises(AssertionError):
        transport.default_transport()


def test_default_transport_is_available_without_the_fixture() -> None:
    assert isinstance(transport.default_transport(), UrllibTransport)


# ---- ReplayTransport -------------------------------------------------------------------------

GRAPHQL = {
    "request": {
        "method": "POST",
        "url": "https://api.test/graphql",
        "json": {"query": "query { viewer { id } }", "variables": {"n": 1}},
    },
    "response": {"status": 200, "json": {"data": {}}},
}
REST = {
    "request": {"method": "GET", "url": "https://api.test/a"},
    "response": {"status": 404},
}


def replay(*exchanges: dict[str, Any]) -> ReplayTransport:
    return ReplayTransport(list(exchanges))


def test_replay_answers_in_order() -> None:
    fake = replay(REST, GRAPHQL)

    first = fake.request("GET", "https://api.test/a", headers={"Authorization": "t"}, timeout=1)
    second = fake.request(
        "POST",
        "https://api.test/graphql",
        headers={"Authorization": "t"},
        json={"query": "query {\n  viewer {\n id }\n}", "variables": {"n": 1}},
        timeout=1,
    )

    assert first == HttpResponse(404, None) and second == HttpResponse(200, {"data": {}})
    fake.assert_authorized()
    fake.finish()


@pytest.mark.parametrize(
    "args",
    [
        ("POST", "https://api.test/a", None),
        ("GET", "https://api.test/b", None),
        ("GET", "https://api.test/a", {"extra": 1}),
    ],
)
def test_replay_rejects_a_mismatched_request(args: tuple[str, str, Any]) -> None:
    fake = replay(REST)

    with pytest.raises(AssertionError):
        fake.request(args[0], args[1], headers={}, json=args[2], timeout=1)

    with pytest.raises(AssertionError):
        fake.finish()


def test_replay_graphql_variables_must_match_exactly() -> None:
    fake = replay(GRAPHQL)

    with pytest.raises(AssertionError):
        fake.request(
            "POST",
            "https://api.test/graphql",
            headers={},
            json={"query": "query { viewer { id } }", "variables": {"n": 2}},
            timeout=1,
        )


def test_replay_extra_request_fails_even_when_swallowed() -> None:
    fake = replay(REST)
    fake.request("GET", "https://api.test/a", headers={}, timeout=1)
    try:
        fake.request("GET", "https://api.test/a", headers={}, timeout=1)
    except AssertionError:
        pass

    with pytest.raises(AssertionError):
        fake.finish()


def test_replay_unconsumed_exchange_fails() -> None:
    with pytest.raises(AssertionError, match="never requested"):
        replay(REST).finish()


def test_replay_requires_authorization_header() -> None:
    fake = replay(REST)
    fake.request("GET", "https://api.test/a", headers={}, timeout=1)

    with pytest.raises(AssertionError, match="Authorization"):
        fake.assert_authorized()


def test_load_fixture_requires_a_source(tmp_path: Path) -> None:
    (tmp_path / "linear").mkdir()
    (tmp_path / "linear" / "s.json").write_text(json.dumps({"exchanges": []}))

    with pytest.raises(AssertionError):
        load_fixture("linear", "s", tmp_path)

    (tmp_path / "linear" / "s.json").write_text(
        json.dumps({"source": "Linear GraphQL schema", "exchanges": [REST]})
    )
    assert ReplayTransport.from_fixture("linear", "s", tmp_path).requests == []


# ---- scenario table and fixture inventory ----------------------------------------------------


def test_scenario_names_are_unique_and_cover_the_acceptance_list() -> None:
    names = [s.name for s in CONTRACT_SCENARIOS]

    assert len(names) == len(set(names))
    common = {s.name for s in CONTRACT_SCENARIOS if s.adapters == ADAPTERS}
    assert {
        "list_assigned", "list_capped_incomplete", "attest_trusted", "attest_untrusted_latest",
        "attest_untrusted_title_edit", "attest_untrusted_body_edit_masked",
        "attest_untrusted_comment_edit_masked", "attest_history_truncated", "attest_unavailable",
        "comment", "state_applied", "state_not_found", "state_permission_denied",
        "close_default", "close_already",
    } == common


def test_adapter_only_scenarios_are_assigned_to_one_adapter() -> None:
    only = {s.name: s.adapters for s in CONTRACT_SCENARIOS if s.adapters != ADAPTERS}

    assert only["close_configured"] == ("jira",)
    assert only["list_slugged_url"] == only["linear_url_mismatch"] == ("linear",)
    assert only["attest_edited_without_history"] == ("github_issues",)
    assert only["attest_trust_lookup_forbidden"] == ("jira",)
    assert len(only) == 10


def test_github_state_scenarios_expect_no_requests() -> None:
    state = [s for s in CONTRACT_SCENARIOS if s.name.startswith("state_")]

    for scenario in state:
        assert scenario.expectation("github_issues") == {
            "status": "unsupported", "reason": "", "requests": 0,
        }
        assert scenario.expectation("linear")["status"] in {"applied", "unavailable"}


def test_jira_close_default_requires_a_merged_state() -> None:
    scenario = next(s for s in CONTRACT_SCENARIOS if s.name == "close_default")

    assert scenario.expectation("jira")["reason"] == "merged_state_required"
    assert scenario.expectation("linear") == {"status": "applied"}


def test_inventory_helpers_compare_names_on_disk(tmp_path: Path) -> None:
    directory = tmp_path / "linear"
    directory.mkdir()
    for name in expected_inventory("linear", ("own_case",)):
        (directory / f"{name}.json").write_text("{}")

    assert fixture_inventory("linear", tmp_path) == expected_inventory("linear", ("own_case",))
    assert_fixture_inventory("linear", ("own_case",), tmp_path)
    (directory / "stray.json").write_text("{}")
    with pytest.raises(AssertionError):
        assert_fixture_inventory("linear", ("own_case",), tmp_path)


def test_scenarios_for_filters_by_adapter() -> None:
    assert "close_configured" in {s.name for s in scenarios_for("jira")}
    assert "close_configured" not in {s.name for s in scenarios_for("linear")}


# ---- run_contract ----------------------------------------------------------------------------


class ScriptedAdapter:
    """Just enough adapter to drive ``run_contract`` over a recorded exchange."""

    def __init__(self, tp: ReplayTransport, trusted: bool | None = True) -> None:
        self.tp = tp
        self.trusted = trusted

    def _get(self) -> Any:
        return self.tp.request("GET", "https://api.test/a", headers={"Authorization": "t"}, timeout=1)

    def list_assigned(self):
        self._get()
        return [], True

    def read(self, ref: str) -> TrackerIssue:
        self._get()
        when = datetime(2026, 10, 1, tzinfo=UTC)
        return TrackerIssue(
            "linear", "1", ref, "https://linear.app/a/issue/ENG-1", "ENG", "t", "b", "alice",
            True, True, (Activity("assigned", "ev" + "a" * 24, when, "alice"),), (), False, "bot",
        )

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        return self.trusted

    def comment(self, ref: str, body: str) -> ActionResult:
        self._get()
        return ActionResult("posted")


def write_fixture(root: Path, adapter: str, scenario: str) -> None:
    directory = root / adapter
    directory.mkdir(exist_ok=True)
    (directory / f"{scenario}.json").write_text(
        json.dumps({"source": "test", "exchanges": [REST]})
    )


def scenario_named(name: str):
    return next(s for s in CONTRACT_SCENARIOS if s.name == name)


@pytest.mark.parametrize("name", ["comment", "attest_trusted", "list_assigned"])
def test_run_contract_replays_and_checks_expectations(tmp_path: Path, name: str) -> None:
    write_fixture(tmp_path, "linear", name)
    scenario = scenario_named(name)

    outcome = run_contract(
        scenario, "linear", lambda tp: ScriptedAdapter(tp), "ENG-1", root=tmp_path  # type: ignore[arg-type,return-value]
    )

    assert_outcome(scenario, "linear", outcome)
    assert outcome.values["requests"] == 1


def test_run_contract_fails_on_unconsumed_exchange(tmp_path: Path) -> None:
    write_fixture(tmp_path, "linear", "state_applied")

    class Idle(ScriptedAdapter):
        def transition(self, ref: str, state: str) -> ActionResult:
            return ActionResult("applied")

    with pytest.raises(AssertionError, match="never requested"):
        run_contract(
            scenario_named("state_applied"), "linear", lambda tp: Idle(tp), "ENG-1", root=tmp_path  # type: ignore[arg-type,return-value]
        )


def test_assert_outcome_detects_a_different_decision(tmp_path: Path) -> None:
    write_fixture(tmp_path, "linear", "attest_trusted")
    scenario = scenario_named("attest_untrusted_latest")
    write_fixture(tmp_path, "linear", scenario.name)

    outcome = run_contract(
        scenario, "linear", lambda tp: ScriptedAdapter(tp), "ENG-1", root=tmp_path  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(AssertionError):
        assert_outcome(scenario, "linear", outcome)
