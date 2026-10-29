"""Linear tracker adapter (GraphQL, OAuth app with actor=app).

The bot is ``viewer.id`` and the issues it works are those whose ``delegate`` is the
viewer. Every change to a rendered field is its own activity attributed to the
actor who made it: each issue history entry yields one activity per change (title,
description, delegate), and a comment yields its creation plus, when edited, one
``comment_edit`` by the comment's user (Linear lets only the author edit).
Activities with no resolvable user have actor ``None`` and are untrusted.

Canonical issue urls are built here: Linear's API url carries a title slug, which is
dropped, and any url that does not name the issue on ``linear.app`` fails closed.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Mapping

from ..config import TrackerAdapterConfig
from ..record import REF_PATTERNS
from ..thread import Activity, Comment, TrackerIssue, activity_id, parse_timestamp
from ..transport import (
    HttpResponse,
    TrackerTransport,
    TrackerUnavailable,
    unavailable_for_status,
)
from .base import REQUEST_TIMEOUT_SECONDS, ActionResult, TrackerAdapter

ADAPTER = "linear"
API_URL = "https://api.linear.app/graphql"
LIST_PAGE_SIZE = 50
CONNECTION_PAGE_SIZE = 100
CLOSED_STATE_TYPES = ("completed", "canceled")

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WORKSPACE_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_FORBIDDEN_CODES = frozenset({"FORBIDDEN", "AUTHENTICATION_ERROR"})

VIEWER_QUERY = "query Viewer { viewer { id } }"
LIST_QUERY = (
    "query AssignedIssues($filter: IssueFilter) {"
    f" issues(filter: $filter, first: {LIST_PAGE_SIZE}, orderBy: updatedAt) {{"
    " nodes { id identifier url updatedAt } pageInfo { hasNextPage } } }"
)
READ_QUERY = (
    "query ReadIssue($id: String!) { issue(id: $id) {"
    " id identifier url title description createdAt creator { id } delegate { id }"
    " state { type } team { key }"
    f" history(first: {CONNECTION_PAGE_SIZE}) {{ pageInfo {{ hasNextPage }}"
    " nodes { id createdAt actor { id } toDelegate { id } fromTitle toTitle updatedDescription } }"
    f" comments(first: {CONNECTION_PAGE_SIZE}) {{ pageInfo {{ hasNextPage }}"
    " nodes { id createdAt editedAt body user { id } } } } }"
)
USER_QUERY = "query TrustedUser($id: String!) { user(id: $id) { id active guest app } }"
WORKFLOW_QUERY = (
    "query IssueWorkflow($id: String!) { issue(id: $id) { id state { name type }"
    " team { states(first: 250) { nodes { id name type } } } } }"
)
COMMENT_MUTATION = (
    "mutation CommentCreate($issueId: String!, $body: String!) {"
    " commentCreate(input: {issueId: $issueId, body: $body}) { success } }"
)
STATE_MUTATION = (
    "mutation SetState($id: String!, $stateId: String!) {"
    " issueUpdate(id: $id, input: {stateId: $stateId}) { success } }"
)


class _Denied(TrackerUnavailable):
    """The tracker refused the credential or the operation."""

    def __init__(self) -> None:
        super().__init__("http_403")


class _NotFound(TrackerUnavailable):
    """Every error in the response says the requested entity does not exist."""

    def __init__(self) -> None:
        super().__init__("malformed_payload")


def _malformed() -> TrackerUnavailable:
    return TrackerUnavailable("malformed_payload")


def _canonical_url(api_url: object, identifier: str) -> str | None:
    """Return the slug-free issue url, or None unless ``api_url`` names ``identifier`` on linear.app."""

    if not isinstance(api_url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(api_url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc != "linear.app" or port is not None:
        return None
    segments = parts.path.split("/")
    if len(segments) < 4 or segments[0] != "":
        return None
    _, workspace, kind, key = segments[:4]
    if kind != "issue" or key != identifier or _WORKSPACE_RE.fullmatch(workspace) is None:
        return None
    return f"https://linear.app/{workspace}/issue/{identifier}"


def _actor(node: object) -> str | None:
    """Return the validated user id of a ``{id}`` node, or None for a null or odd one."""

    if not isinstance(node, dict):
        return None
    value = node.get("id")
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _malformed()
    return value


def _nodes(connection: object) -> tuple[list[dict[str, Any]], bool]:
    data = _mapping(connection)
    nodes = data.get("nodes")
    page_info = _mapping(data.get("pageInfo"))
    has_next = page_info.get("hasNextPage")
    if not isinstance(nodes, list) or not isinstance(has_next, bool):
        raise _malformed()
    return [_mapping(node) for node in nodes], has_next


def _timestamp(value: object):
    try:
        return parse_timestamp(value)
    except ValueError:
        raise _malformed() from None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class LinearAdapter:
    def __init__(
        self,
        config: TrackerAdapterConfig,
        api_key: str,
        transport: TrackerTransport,
    ) -> None:
        self._config = config
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._transport = transport
        self._trust_cache: dict[str, bool] = {}
        viewer = _mapping(_mapping(self._graphql(VIEWER_QUERY, {})).get("viewer"))
        bot_id = _actor(viewer)
        if bot_id is None:
            raise ValueError("linear viewer has no usable id")
        self._bot_id = bot_id

    def _send(self, query: str, variables: Mapping[str, Any]) -> HttpResponse:
        return self._transport.request(
            "POST",
            API_URL,
            headers=self._headers,
            json={"query": query, "variables": dict(variables)},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """Run one query and return its ``data``; any error body fails closed."""

        response = self._send(query, variables)
        if response.status in (401, 403):
            raise _Denied()
        if response.status != 200:
            raise unavailable_for_status(response.status)
        body = _mapping(response.json)
        errors = body.get("errors")
        if errors:
            if isinstance(errors, list) and any(_is_forbidden(error) for error in errors):
                raise _Denied()
            if isinstance(errors, list) and any(_is_rate_limited(error) for error in errors):
                raise TrackerUnavailable("rate_limited")
            if isinstance(errors, list) and all(_is_not_found(error) for error in errors):
                raise _NotFound()
            raise _malformed()
        return _mapping(body.get("data"))

    def _in_scope(self, identifier: str) -> bool:
        teams = self._config.team_keys
        return not teams or identifier.rsplit("-", 1)[0] in teams

    def list_assigned(self) -> tuple[list[TrackerIssue], bool]:
        filter_: dict[str, Any] = {
            "delegate": {"id": {"eq": self._bot_id}},
            "state": {"type": {"nin": list(CLOSED_STATE_TYPES)}},
        }
        if self._config.team_keys:
            filter_["team"] = {"key": {"in": list(self._config.team_keys)}}
        nodes, has_next = _nodes(
            _mapping(self._graphql(LIST_QUERY, {"filter": filter_})).get("issues")
        )
        complete = not has_next
        listed: list[tuple[Any, TrackerIssue]] = []
        for node in nodes:
            stable_id = node.get("id")
            identifier = node.get("identifier")
            if (
                not isinstance(stable_id, str)
                or _ID_RE.fullmatch(stable_id) is None
                or not isinstance(identifier, str)
                or REF_PATTERNS[ADAPTER].fullmatch(identifier) is None
            ):
                complete = False
                continue
            url = _canonical_url(node.get("url"), identifier)
            if url is None:
                complete = False
                continue
            try:
                updated = parse_timestamp(node.get("updatedAt"))
            except ValueError:
                complete = False
                continue
            if not self._in_scope(identifier):
                continue
            listed.append(
                (updated, self._shell(stable_id, identifier, url, updated.isoformat()))
            )
        listed.sort(key=lambda pair: pair[0], reverse=True)
        return [issue for _, issue in listed], complete

    def _shell(self, stable_id: str, identifier: str, url: str, updated_at: str) -> TrackerIssue:
        return TrackerIssue(
            adapter=ADAPTER,
            stable_id=stable_id,
            ref=identifier,
            url=url,
            project=identifier.rsplit("-", 1)[0],
            title="",
            body="",
            author_id=None,
            is_open=True,
            assigned_to_bot=True,
            activities=(),
            comments=(),
            truncated=False,
            bot_id=self._bot_id,
            updated_at=updated_at,
        )

    def read(self, ref: str) -> TrackerIssue:
        if REF_PATTERNS[ADAPTER].fullmatch(ref) is None:
            raise ValueError(f"not a linear ref: {ref!r}")
        issue = _mapping(_mapping(self._graphql(READ_QUERY, {"id": ref})).get("issue"))
        stable_id = issue.get("id")
        identifier = issue.get("identifier")
        if (
            not isinstance(stable_id, str)
            or _ID_RE.fullmatch(stable_id) is None
            or not isinstance(identifier, str)
            or REF_PATTERNS[ADAPTER].fullmatch(identifier) is None
        ):
            raise _malformed()
        url = _canonical_url(issue.get("url"), identifier)
        if url is None:
            raise _malformed()
        history, history_more = _nodes(issue.get("history"))
        comment_nodes, comments_more = _nodes(issue.get("comments"))

        activities: list[Activity] = []
        for entry in history:
            activities.extend(self._history_activities(entry))
        comments: list[Comment] = []
        for node in comment_nodes:
            comment_id = node.get("id")
            if not isinstance(comment_id, str) or not comment_id:
                raise _malformed()
            author = _actor(node.get("user"))
            created = _timestamp(node.get("createdAt"))
            comments.append(Comment(comment_id, author, created, _text(node.get("body"))))
            activities.append(Activity("comment", activity_id(ADAPTER, comment_id), created, author))
            edited_at = node.get("editedAt")
            if edited_at is not None:
                activities.append(
                    Activity(
                        "comment_edit",
                        activity_id(ADAPTER, f"{comment_id}:edited:{edited_at}"),
                        _timestamp(edited_at),
                        author,
                    )
                )
        comments.sort(key=lambda c: c.at)

        state_type = _mapping(issue.get("state")).get("type")
        team = _mapping(issue.get("team")).get("key")
        return TrackerIssue(
            adapter=ADAPTER,
            stable_id=stable_id,
            ref=identifier,
            url=url,
            project=team if isinstance(team, str) else identifier.rsplit("-", 1)[0],
            title=_text(issue.get("title")),
            body=_text(issue.get("description")),
            author_id=_actor(issue.get("creator")),
            is_open=isinstance(state_type, str) and state_type not in CLOSED_STATE_TYPES,
            assigned_to_bot=_actor(issue.get("delegate")) == self._bot_id,
            activities=tuple(sorted(activities, key=lambda a: (a.at, a.id))),
            comments=tuple(comments),
            truncated=history_more or comments_more,
            bot_id=self._bot_id,
        )

    def _history_activities(self, entry: Mapping[str, Any]) -> list[Activity]:
        history_id = entry.get("id")
        if not isinstance(history_id, str) or not history_id:
            raise _malformed()
        at = _timestamp(entry.get("createdAt"))
        actor = _actor(entry.get("actor"))
        found: list[Activity] = []
        if _actor(entry.get("toDelegate")) == self._bot_id:
            found.append(Activity("assigned", activity_id(ADAPTER, f"{history_id}:assigned"), at, actor))
        if entry.get("fromTitle") is not None or entry.get("toTitle") is not None:
            found.append(
                Activity("title_edit", activity_id(ADAPTER, f"{history_id}:title_edit"), at, actor)
            )
        if entry.get("updatedDescription"):
            found.append(
                Activity("body_edit", activity_id(ADAPTER, f"{history_id}:body_edit"), at, actor)
            )
        return found

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        """Trusted means an active, non-guest member; apps and unknown users are not."""

        if _ID_RE.fullmatch(actor_id) is None:
            return False
        if actor_id not in self._trust_cache:
            try:
                user = _mapping(self._graphql(USER_QUERY, {"id": actor_id})).get("user")
            except _NotFound:
                user = None
            if user is None:
                verdict = False
            else:
                user = _mapping(user)
                verdict = (
                    user.get("id") == actor_id
                    and user.get("active") is True
                    and user.get("guest") is False
                    and user.get("app") is False
                )
            self._trust_cache[actor_id] = verdict
        return self._trust_cache[actor_id]

    def _workflow(self, ref: str) -> dict[str, Any]:
        return _mapping(_mapping(self._graphql(WORKFLOW_QUERY, {"id": ref})).get("issue"))

    def comment(self, ref: str, body: str) -> ActionResult:
        try:
            issue_id = self._workflow(ref).get("id")
            if not isinstance(issue_id, str):
                raise _malformed()
            data = self._graphql(COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        except _Denied:
            return ActionResult("unavailable", "permission_denied")
        created = _mapping(data.get("commentCreate")).get("success") is True
        return ActionResult("posted") if created else ActionResult("unavailable", "api_error")

    def transition(self, ref: str, state: str) -> ActionResult:
        try:
            issue = self._workflow(ref)
            states = _states(issue)
            current = _mapping(issue.get("state")).get("name")
            matches = [s for s in states if s.get("name") == state]
            if len(matches) > 1:
                return ActionResult("unavailable", "ambiguous_state")
            if not matches:
                return ActionResult("unavailable", "state_not_found")
            if current == state:
                return ActionResult("already")
            return self._set_state(ref, matches[0])
        except _Denied:
            return ActionResult("unavailable", "permission_denied")

    def close(self, ref: str) -> ActionResult:
        try:
            issue = self._workflow(ref)
            states = _states(issue)
            current = _mapping(issue.get("state"))
            target_name = self._merged_state(ref)
            if target_name is None:
                if current.get("type") == "completed":
                    return ActionResult("already")
                targets = [s for s in states if s.get("type") == "completed"]
                if not targets:
                    return ActionResult("unavailable", "state_not_found")
                if len(targets) > 1:
                    return ActionResult("unavailable", "merged_state_required")
            else:
                if current.get("name") == target_name:
                    return ActionResult("already")
                targets = [s for s in states if s.get("name") == target_name]
                if len(targets) > 1:
                    return ActionResult("unavailable", "ambiguous_state")
                if not targets:
                    return ActionResult("unavailable", "state_not_found")
            return self._set_state(ref, targets[0])
        except _Denied:
            return ActionResult("unavailable", "permission_denied")

    def _merged_state(self, ref: str) -> str | None:
        scoped = self._config.project_transitions.get(ref.rsplit("-", 1)[0], {})
        return scoped.get("merged") or self._config.transitions.get("merged")

    def _set_state(self, ref: str, state: Mapping[str, Any]) -> ActionResult:
        state_id = state.get("id")
        if not isinstance(state_id, str):
            raise _malformed()
        data = self._graphql(STATE_MUTATION, {"id": ref, "stateId": state_id})
        applied = _mapping(data.get("issueUpdate")).get("success") is True
        return ActionResult("applied") if applied else ActionResult("unavailable", "api_error")

    def current_state(self, ref: str) -> str | None:
        name = _mapping(self._workflow(ref).get("state")).get("name")
        return name if isinstance(name, str) else None


def _is_forbidden(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    return isinstance(extensions, dict) and extensions.get("code") in _FORBIDDEN_CODES


def _is_rate_limited(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    return isinstance(extensions, dict) and extensions.get("code") == "RATELIMITED"


def _is_not_found(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    message = error.get("message")
    return isinstance(message, str) and message.startswith("Entity not found")


def _states(issue: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = _mapping(_mapping(issue.get("team")).get("states")).get("nodes")
    if not isinstance(nodes, list):
        raise _malformed()
    return [_mapping(node) for node in nodes]


def build(
    config: TrackerAdapterConfig, env: Mapping[str, str], transport: TrackerTransport
) -> TrackerAdapter:
    """Build the adapter; it refuses to exist without a credential and a bot identity."""

    api_key = env.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        raise ValueError("LINEAR_API_KEY is required for the linear tracker adapter")
    return LinearAdapter(config, api_key, transport)
