"""The Jira Cloud tracker adapter (REST API v3, basic auth).

Every change to a rendered field is its own activity attributed to the person who
made it. Jira keeps no comment edit history, only the last editor, so an edited
comment is a ``comment_edit`` with no actor: the delivery rule withholds the issue
when one is newer than the cutoff, whoever the comment's author or ``updateAuthor``
is. ``updateAuthor`` is deliberately never read.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Mapping

from mimir.poller_tracker import safe_url

from ..config import TrackerAdapterConfig
from ..thread import Activity, ActivityKind, Comment, TrackerIssue, activity_id, parse_timestamp
from ..transport import HttpResponse, TrackerTransport, TrackerUnavailable, unavailable_for_status
from .base import REQUEST_TIMEOUT_SECONDS, ActionResult

ADAPTER = "jira"
SEARCH_PAGE_SIZE = 50
_ISSUE_FIELDS = "summary,description,status,assignee,project,creator,comment,updated"
_SITE_RE = re.compile(r"[a-z0-9.-]{1,253}")
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*")
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9:_-]{1,128}")
_STABLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ROLE_ID_RE = re.compile(r"/role/([0-9]{1,20})$")
_HISTORY_KINDS: Mapping[str, ActivityKind] = {"summary": "title_edit", "description": "body_edit"}
_ADF_MAX_DEPTH = 32
_ADF_BLOCKS = frozenset(
    {
        "paragraph", "heading", "blockquote", "codeBlock", "bulletList", "orderedList",
        "listItem", "panel", "rule", "table", "tableRow", "tableCell", "tableHeader",
        "mediaSingle", "mediaGroup", "expand", "decisionList", "decisionItem",
        "taskList", "taskItem",
    }
)


def build(config: TrackerAdapterConfig, env: Mapping[str, str], transport: TrackerTransport) -> "JiraAdapter":
    """Return the Jira adapter; missing credentials or identity raise ValueError."""

    site = env.get("JIRA_SITE", "")
    email = env.get("JIRA_EMAIL", "")
    token = env.get("JIRA_API_TOKEN", "")
    if _SITE_RE.fullmatch(site) is None:
        raise ValueError("JIRA_SITE must be a lower-case host name")
    if not email or not token:
        raise ValueError("JIRA_EMAIL and JIRA_API_TOKEN are required")
    if _ACCOUNT_ID_RE.fullmatch(config.account_id) is None:
        raise ValueError("jira account_id is required and must be a valid accountId")
    if not config.projects:
        raise ValueError("jira projects are required")
    return JiraAdapter(config, site, email, token, transport)


class JiraAdapter:
    def __init__(
        self,
        config: TrackerAdapterConfig,
        site: str,
        email: str,
        token: str,
        transport: TrackerTransport,
    ) -> None:
        self._config = config
        self._site = site
        self._transport = transport
        self._bot_id = config.account_id
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        }
        self._active: dict[str, bool] = {}
        self._groups: dict[str, frozenset[str]] = {}
        self._role_ids: dict[str, Mapping[str, str]] = {}
        self._role_actors: dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]] = {}

    def _url(self, path: str) -> str:
        return f"https://{self._site}/rest/api/3/{path}"

    def _call(self, method: str, path: str, body: Any = None) -> HttpResponse:
        return self._transport.request(
            method, self._url(path), headers=self._headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )

    def _canonical_url(self, ref: str) -> str | None:
        return safe_url(f"https://{self._site}/browse/{ref}", ref)

    def _valid_ref(self, ref: str) -> bool:
        return _KEY_RE.fullmatch(ref) is not None and ref.rsplit("-", 1)[0] in self._config.projects

    def list_assigned(self) -> tuple[list[TrackerIssue], bool]:
        projects = ", ".join(_jql_string(project) for project in self._config.projects)
        jql = (
            f'assignee = "{self._bot_id}" AND statusCategory != Done '
            f"AND project in ({projects}) ORDER BY updated DESC"
        )
        response = self._call(
            "POST",
            "search/jql",
            {"jql": jql, "maxResults": SEARCH_PAGE_SIZE, "fields": ["updated"]},
        )
        data = _ok_json(response)
        rows = data.get("issues")
        if not isinstance(rows, list):
            raise TrackerUnavailable("malformed_payload")
        complete = data.get("isLast") is True and not data.get("nextPageToken")
        issues: list[TrackerIssue] = []
        for row in rows:
            issue = self._listed(row)
            if issue is None:
                complete = False
            else:
                issues.append(issue)
        return issues, complete

    def _listed(self, row: Any) -> TrackerIssue | None:
        if not isinstance(row, dict):
            return None
        key, raw_id = row.get("key"), row.get("id")
        if not isinstance(key, str) or not self._valid_ref(key):
            return None
        if not isinstance(raw_id, str) or _STABLE_ID_RE.fullmatch(raw_id) is None:
            return None
        url = self._canonical_url(key)
        if url is None:
            return None
        try:
            updated_at = parse_timestamp(_mapping(row.get("fields")).get("updated")).isoformat()
        except ValueError:
            return None
        return TrackerIssue(
            adapter=ADAPTER,
            stable_id=raw_id,
            ref=key,
            url=url,
            project=key.rsplit("-", 1)[0],
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
        if not self._valid_ref(ref):
            raise ValueError("ref is not a configured jira issue key")
        data = _ok_json(self._call("GET", f"issue/{ref}?expand=changelog&fields={_ISSUE_FIELDS}"))
        fields = _mapping(data.get("fields"))
        project = ref.rsplit("-", 1)[0]
        raw_id = data.get("id")
        url = self._canonical_url(ref)
        if (
            data.get("key") != ref
            or _mapping(fields.get("project")).get("key") != project
            or not isinstance(raw_id, str)
            or _STABLE_ID_RE.fullmatch(raw_id) is None
            or url is None
        ):
            raise TrackerUnavailable("malformed_payload")
        changelog = _mapping(data.get("changelog"))
        histories = _list(changelog.get("histories"))
        comment_block = _mapping(fields.get("comment"))
        raw_comments = _list(comment_block.get("comments"))
        try:
            activities = self._history_activities(histories)
            comments, edits = _comments(raw_comments)
        except ValueError:
            raise TrackerUnavailable("malformed_payload") from None
        status = _mapping(fields.get("status"))
        category = _mapping(status.get("statusCategory")).get("key")
        summary, description = fields.get("summary"), fields.get("description")
        if not isinstance(summary, str) or not isinstance(category, str):
            raise TrackerUnavailable("malformed_payload")
        return TrackerIssue(
            adapter=ADAPTER,
            stable_id=raw_id,
            ref=ref,
            url=url,
            project=project,
            title=summary,
            body=_adf_text(description),
            author_id=_account(fields.get("creator")),
            is_open=category != "done",
            assigned_to_bot=_account(fields.get("assignee")) == self._bot_id,
            activities=tuple(activities)
            + tuple(Activity("comment", activity_id(ADAPTER, c.id), c.at, c.author_id) for c in comments)
            + tuple(edits),
            comments=tuple(comments),
            truncated=_total(changelog, histories) or _total(comment_block, raw_comments),
            bot_id=self._bot_id,
        )

    def _history_activities(self, histories: list[Any]) -> list[Activity]:
        activities: list[Activity] = []
        for history in histories:
            entry = _mapping(history)
            history_id = entry.get("id")
            if not isinstance(history_id, str):
                raise ValueError("history id")
            at = parse_timestamp(entry.get("created"))
            actor = _account(entry.get("author"))
            seen: set[ActivityKind] = set()
            for raw_item in _list(entry.get("items")):
                item = _mapping(raw_item)
                field = item.get("field")
                if field == "assignee":
                    if item.get("to") != self._bot_id:
                        continue
                    kind: ActivityKind = "assigned"
                elif isinstance(field, str) and field in _HISTORY_KINDS:
                    kind = _HISTORY_KINDS[field]
                else:
                    continue
                if kind in seen:
                    continue
                seen.add(kind)
                activities.append(
                    Activity(kind, activity_id(ADAPTER, f"{history_id}:{kind}"), at, actor)
                )
        return activities

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        if _ACCOUNT_ID_RE.fullmatch(actor_id) is None or actor_id == self._bot_id:
            return False
        if not self._is_active(actor_id):
            return False
        config = self._config
        if config.trusted_groups and not set(config.trusted_groups) & self._groups_of(actor_id):
            if not config.trusted_project_roles:
                return False
        elif config.trusted_groups:
            return True
        return any(
            self._in_role(actor_id, issue.project or issue.ref.rsplit("-", 1)[0], role)
            for role in config.trusted_project_roles
        )

    def _trust_get(self, path: str) -> dict[str, Any] | list[Any] | None:
        response = self._call("GET", path)
        if response.status in (401, 403):
            raise TrackerUnavailable("http_403")
        if response.status == 404:
            return None
        if response.status != 200:
            raise unavailable_for_status(response.status)
        if not isinstance(response.json, (dict, list)):
            raise TrackerUnavailable("malformed_payload")
        return response.json

    def _is_active(self, actor_id: str) -> bool:
        if actor_id not in self._active:
            data = self._trust_get(f"user?accountId={urllib.parse.quote(actor_id, safe='')}")
            self._active[actor_id] = isinstance(data, dict) and data.get("active") is True
        return self._active[actor_id]

    def _groups_of(self, actor_id: str) -> frozenset[str]:
        if actor_id not in self._groups:
            data = self._trust_get(f"user/groups?accountId={urllib.parse.quote(actor_id, safe='')}")
            if not isinstance(data, list):
                raise TrackerUnavailable("malformed_payload")
            self._groups[actor_id] = frozenset(
                name for group in data if isinstance(name := _mapping(group).get("name"), str)
            )
        return self._groups[actor_id]

    def _in_role(self, actor_id: str, project: str, role: str) -> bool:
        if project not in self._role_ids:
            data = self._trust_get(f"project/{project}/role")
            if not isinstance(data, dict):
                raise TrackerUnavailable("malformed_payload")
            ids: dict[str, str] = {}
            for name, link in data.items():
                match = _ROLE_ID_RE.search(link) if isinstance(link, str) else None
                if match is not None:
                    ids[name] = match.group(1)
            self._role_ids[project] = ids
        role_id = self._role_ids[project].get(role)
        if role_id is None:
            return False
        if (project, role_id) not in self._role_actors:
            data = self._trust_get(f"project/{project}/role/{role_id}")
            if not isinstance(data, dict):
                raise TrackerUnavailable("malformed_payload")
            users: set[str] = set()
            groups: set[str] = set()
            for actor in _list(data.get("actors")):
                entry = _mapping(actor)
                if (account := _account(entry.get("actorUser"))) is not None:
                    users.add(account)
                if isinstance(name := _mapping(entry.get("actorGroup")).get("name"), str):
                    groups.add(name)
            self._role_actors[(project, role_id)] = (frozenset(users), frozenset(groups))
        users_in_role, groups_in_role = self._role_actors[(project, role_id)]
        if actor_id in users_in_role:
            return True
        return bool(groups_in_role) and bool(groups_in_role & self._groups_of(actor_id))

    def comment(self, ref: str, body: str) -> ActionResult:
        if not self._valid_ref(ref):
            return ActionResult("refused", "project_not_configured")
        response = self._call("POST", f"issue/{ref}/comment", {"body": _adf_document(body)})
        if response.status == 201:
            return ActionResult("posted")
        return _failed(response)

    def current_state(self, ref: str) -> str | None:
        if not self._valid_ref(ref):
            return None
        response = self._call("GET", f"issue/{ref}?fields=status")
        if response.status == 404:
            return None
        name = _mapping(_mapping(_ok_json(response).get("fields")).get("status")).get("name")
        return name if isinstance(name, str) else None

    def transition(self, ref: str, state: str) -> ActionResult:
        if not self._valid_ref(ref):
            return ActionResult("refused", "project_not_configured")
        return self._move(ref, state)

    def _apply(self, ref: str, state: str) -> ActionResult:
        response = self._call("GET", f"issue/{ref}/transitions")
        if response.status == 403:
            return ActionResult("unavailable", "permission_denied")
        matches = [
            transition
            for item in _list(_ok_json(response).get("transitions"))
            if _mapping(_mapping(item).get("to")).get("name") == state
            and isinstance(transition := _mapping(item).get("id"), str)
        ]
        if not matches:
            return ActionResult("unavailable", "state_not_found")
        if len(matches) > 1:
            return ActionResult("unavailable", "state_ambiguous")
        response = self._call("POST", f"issue/{ref}/transitions", {"transition": {"id": matches[0]}})
        if response.status in (200, 204):
            return ActionResult("applied")
        return _failed(response)

    def close(self, ref: str) -> ActionResult:
        if not self._valid_ref(ref):
            return ActionResult("refused", "project_not_configured")
        project = ref.rsplit("-", 1)[0]
        target = self._config.project_transitions.get(project, {}).get(
            "merged"
        ) or self._config.transitions.get("merged")
        if not target:
            return ActionResult("unavailable", "merged_state_required")
        return self._move(ref, target)

    def _move(self, ref: str, state: str) -> ActionResult:
        try:
            current = self.current_state(ref)
        except TrackerUnavailable as error:
            if error.code == "http_403":
                return ActionResult("unavailable", "permission_denied")
            raise
        if current == state:
            return ActionResult("already")
        return self._apply(ref, state)


def _jql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _failed(response: HttpResponse) -> ActionResult:
    if response.status == 403:
        return ActionResult("unavailable", "permission_denied")
    if response.status == 404:
        return ActionResult("unavailable", "not_found")
    raise unavailable_for_status(response.status)


def _ok_json(response: HttpResponse) -> dict[str, Any]:
    if response.status != 200:
        raise unavailable_for_status(response.status)
    if not isinstance(response.json, dict):
        raise TrackerUnavailable("malformed_payload")
    return response.json


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _account(user: Any) -> str | None:
    account = _mapping(user).get("accountId")
    if isinstance(account, str) and _ACCOUNT_ID_RE.fullmatch(account):
        return account
    return None


def _total(block: Mapping[str, Any], items: list[Any]) -> bool:
    total = block.get("total")
    return isinstance(total, int) and not isinstance(total, bool) and total > len(items)


def _comments(raw: list[Any]) -> tuple[list[Comment], list[Activity]]:
    comments: list[Comment] = []
    edits: list[Activity] = []
    for item in raw:
        entry = _mapping(item)
        comment_id = entry.get("id")
        if not isinstance(comment_id, str):
            raise ValueError("comment id")
        created_text, updated_text = entry.get("created"), entry.get("updated")
        created = parse_timestamp(created_text)
        comments.append(
            Comment(comment_id, _account(entry.get("author")), created, _adf_text(entry.get("body")))
        )
        updated = parse_timestamp(updated_text)
        if updated != created:
            edits.append(
                Activity(
                    "comment_edit",
                    activity_id(ADAPTER, f"{comment_id}:edited:{updated_text}"),
                    updated,
                    None,
                )
            )
    return comments, edits


def _adf_text(node: Any) -> str:
    """Return the plain text of an Atlassian Document Format value, deterministically."""

    if isinstance(node, str):
        return node
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            lines.append("".join(current))
            current.clear()

    def walk(value: Any, depth: int) -> None:
        if depth > _ADF_MAX_DEPTH or not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind == "text" and isinstance(value.get("text"), str):
            current.append(value["text"])
        elif kind == "hardBreak":
            flush()
        elif kind == "mention":
            text = _mapping(value.get("attrs")).get("text")
            current.append(text if isinstance(text, str) else "")
        else:
            block = kind in _ADF_BLOCKS
            if block:
                flush()
            for child in _list(value.get("content")):
                walk(child, depth + 1)
            if block:
                flush()

    walk(node, 0)
    flush()
    return "\n".join(lines)


def _adf_document(text: str) -> dict[str, Any]:
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"}
        for line in text.split("\n")
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


__all__ = ["JiraAdapter", "build"]
