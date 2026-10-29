"""GitHub Issues tracker adapter.

Lists and comments over REST and reads a thread over one GraphQL query. Every
rendered field change is its own activity attributed to the login that made it;
a null or ghost login is unattributable (``None``). Logins are casefolded so
they compare equal to the bot identity.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from mimir.poller_tracker import safe_url

from ..config import TrackerAdapterConfig
from ..record import REF_PATTERNS
from ..thread import Activity, Comment, TrackerIssue, activity_id, parse_timestamp
from ..transport import TrackerTransport, TrackerUnavailable, unavailable_for_status
from .base import REQUEST_TIMEOUT_SECONDS, ActionResult, TrackerAdapter

ADAPTER = "github_issues"
API = "https://api.github.com"
LIST_PAGE_SIZE = 50

_EDITS = "userContentEdits(first:100){totalCount pageInfo{hasNextPage} nodes{id editedAt editor{login}}}"
_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{issue(number:$number){id databaseId number title body state createdAt author{login} "
    f"lastEditedAt {_EDITS} assignees(first:20){{nodes{{login}}}} "
    "timelineItems(first:100, itemTypes:[ASSIGNED_EVENT, ISSUE_COMMENT, RENAMED_TITLE_EVENT])"
    "{totalCount pageInfo{hasNextPage} nodes{__typename "
    "... on AssignedEvent{id createdAt actor{login} assignee{... on User{login} ... on Bot{login}}} "
    f"... on IssueComment{{id createdAt author{{login}} body lastEditedAt {_EDITS}}} "
    "... on RenamedTitleEvent{id createdAt actor{login}}}}}}}"
)


def build(
    config: TrackerAdapterConfig, env: Mapping[str, str], transport: TrackerTransport
) -> TrackerAdapter:
    """Return the adapter; a missing token or bot identity raises ValueError.

    An empty bot identity would make the delivery rule skip no activity, so it
    is refused rather than defaulted.
    """

    token = env.get("GITHUB_TOKEN", "").strip()
    login = env.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if not token:
        raise ValueError("github_issues needs GITHUB_TOKEN")
    if not login or not login.isprintable() or any(c.isspace() for c in login):
        raise ValueError("github_issues needs MIMIR_GITHUB_SELF_LOGIN")
    if not config.repos:
        raise ValueError("github_issues needs at least one repository")
    return GithubIssuesAdapter(config.repos, token, login, transport)


class GithubIssuesAdapter:
    def __init__(
        self, repos: Sequence[str], token: str, login: str, transport: TrackerTransport
    ) -> None:
        self._repos = tuple(repo.casefold() for repo in repos)
        self._login = login
        self._bot_id = login.casefold()
        self._token = token
        self._transport = transport

    def list_assigned(self) -> tuple[list[TrackerIssue], bool]:
        issues: list[TrackerIssue] = []
        complete = True
        for repo in self._repos:
            query = urllib.parse.urlencode(
                {
                    "assignee": self._login,
                    "state": "open",
                    "per_page": LIST_PAGE_SIZE,
                    "sort": "updated",
                    "direction": "desc",
                }
            )
            data = self._ok_json("GET", f"{API}/repos/{repo}/issues?{query}")
            if not isinstance(data, list):
                raise TrackerUnavailable("malformed_payload")
            complete = complete and len(data) < LIST_PAGE_SIZE
            for item in data:
                if not isinstance(item, Mapping):
                    complete = False
                elif "pull_request" in item:
                    continue
                else:
                    issue = self._listed(repo, item)
                    if issue is None:
                        complete = False
                    else:
                        issues.append(issue)
        return issues, complete

    def read(self, ref: str) -> TrackerIssue:
        repo, number = self._target(ref)
        owner, name = repo.split("/")
        body = {"query": _QUERY, "variables": {"owner": owner, "name": name, "number": number}}
        data = self._ok_json("POST", f"{API}/graphql", body)
        if isinstance(data, Mapping) and data.get("errors"):
            errors = data["errors"]
            limited = isinstance(errors, list) and any(
                isinstance(e, Mapping) and e.get("type") == "RATE_LIMITED" for e in errors
            )
            raise TrackerUnavailable("rate_limited" if limited else "malformed_payload")
        try:
            return self._parse(repo, number, data["data"]["repository"]["issue"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TrackerUnavailable("malformed_payload") from None

    def trust(self, actor_id: str, issue: TrackerIssue) -> bool | None:
        from mimir import pollers

        repo = issue.ref.split("#", 1)[0]
        verdict = pollers._github_author_is_trusted(
            repo, actor_id, self._token, timeout=REQUEST_TIMEOUT_SECONDS
        )
        return verdict if isinstance(verdict, bool) else None

    def comment(self, ref: str, body: str) -> ActionResult:
        repo, number = self._target(ref)
        response = self._transport.request(
            "POST",
            f"{API}/repos/{repo}/issues/{number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status in (200, 201):
            return ActionResult("posted")
        return _failure(response.status)

    def transition(self, ref: str, state: str) -> ActionResult:
        self._target(ref)
        return ActionResult("unsupported")

    def close(self, ref: str) -> ActionResult:
        repo, number = self._target(ref)
        url = f"{API}/repos/{repo}/issues/{number}"
        current = self._transport.request(
            "GET", url, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
        if current.status != 200:
            return _failure(current.status)
        state = current.json.get("state") if isinstance(current.json, Mapping) else None
        if state == "closed":
            return ActionResult("already")
        if state != "open":
            return ActionResult("unavailable", "api_error")
        response = self._transport.request(
            "PATCH",
            url,
            headers=self._headers(),
            json={"state": "closed", "state_reason": "completed"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status == 200:
            return ActionResult("applied")
        return _failure(response.status)

    def current_state(self, ref: str) -> str | None:
        repo, number = self._target(ref)
        data = self._ok_json("GET", f"{API}/repos/{repo}/issues/{number}")
        state = data.get("state") if isinstance(data, Mapping) else None
        return state if isinstance(state, str) else None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mimir-cultivate-tracker",
        }

    def _ok_json(self, method: str, url: str, body: Any = None) -> Any:
        response = self._transport.request(
            method, url, headers=self._headers(), json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status != 200:
            raise unavailable_for_status(response.status)
        return response.json

    def _target(self, ref: str) -> tuple[str, int]:
        """Split ``ref`` into a configured repository and issue number, or raise ValueError."""

        if not isinstance(ref, str) or REF_PATTERNS[ADAPTER].fullmatch(ref) is None:
            raise ValueError("invalid github_issues ref")
        repo, _, number = ref.partition("#")
        if repo.casefold() not in self._repos:
            raise ValueError("github_issues ref is outside the configured repositories")
        return repo.casefold(), int(number)

    def _actor(self, node: object) -> str | None:
        if node is None:
            return None
        login = node["login"] if isinstance(node, Mapping) else None
        if not isinstance(login, str) or not login:
            raise ValueError("actor has no login")
        folded = login.casefold()
        return None if folded == "ghost" else folded

    def _listed(self, repo: str, item: Mapping[str, Any]) -> TrackerIssue | None:
        number, database_id, title = item.get("number"), item.get("id"), item.get("title")
        if (
            not _is_positive_int(number)
            or not _is_positive_int(database_id)
            or not isinstance(title, str)
        ):
            return None
        ref = f"{repo}#{number}"
        url = _canonical_url(repo, number, ref)
        if url is None:
            return None
        return TrackerIssue(
            adapter=ADAPTER, stable_id=str(database_id), ref=ref, url=url, project=None,
            title=title, body="", author_id=None, is_open=True, assigned_to_bot=True,
            activities=(), comments=(), truncated=False, bot_id=self._bot_id,
        )

    def _parse(self, repo: str, number: int, node: Any) -> TrackerIssue:
        if not isinstance(node, Mapping) or node["number"] != number:
            raise ValueError("issue payload does not match the ref")
        database_id = node["databaseId"]
        if not _is_positive_int(database_id):
            raise ValueError("issue has no database id")
        ref = f"{repo}#{number}"
        url = _canonical_url(repo, number, ref)
        if url is None:
            raise ValueError("no canonical url")
        state = node["state"]
        if state not in ("OPEN", "CLOSED"):
            raise ValueError("unknown issue state")
        title, body = node["title"], node["body"] or ""
        if not isinstance(title, str) or not isinstance(body, str):
            raise ValueError("issue text is not a string")
        truncated = _edits_truncated(node)
        activities = self._edit_activities("body_edit", node)
        comments: list[Comment] = []
        timeline = node["timelineItems"]
        truncated = truncated or _connection_truncated(timeline)
        for entry in timeline["nodes"]:
            kind = entry["__typename"]
            at = parse_timestamp(entry["createdAt"])
            if kind == "AssignedEvent":
                assignee = entry["assignee"]
                if (
                    isinstance(assignee, Mapping)
                    and str(assignee.get("login", "")).casefold() == self._bot_id
                ):
                    activities.append(
                        Activity(
                            "assigned", activity_id(ADAPTER, entry["id"]), at,
                            self._actor(entry["actor"]),
                        )
                    )
            elif kind == "RenamedTitleEvent":
                activities.append(
                    Activity(
                        "title_edit", activity_id(ADAPTER, entry["id"]), at,
                        self._actor(entry["actor"]),
                    )
                )
            elif kind == "IssueComment":
                author = self._actor(entry["author"])
                text = entry["body"] or ""
                if not isinstance(text, str):
                    raise ValueError("comment body is not a string")
                comment_id = activity_id(ADAPTER, entry["id"])
                comments.append(Comment(comment_id, author, at, text))
                activities.append(Activity("comment", comment_id, at, author))
                activities.extend(self._edit_activities("comment_edit", entry))
                truncated = truncated or _edits_truncated(entry)
            else:
                raise ValueError("unexpected timeline item")
        assignees = node["assignees"]["nodes"]
        return TrackerIssue(
            adapter=ADAPTER,
            stable_id=str(database_id),
            ref=ref,
            url=url,
            project=None,
            title=title,
            body=body,
            author_id=self._actor(node["author"]),
            is_open=state == "OPEN",
            assigned_to_bot=any(
                isinstance(a, Mapping) and str(a.get("login", "")).casefold() == self._bot_id
                for a in assignees
            ),
            activities=tuple(activities),
            comments=tuple(comments),
            truncated=truncated,
            bot_id=self._bot_id,
        )

    def _edit_activities(self, kind: Any, node: Mapping[str, Any]) -> list[Activity]:
        return [
            Activity(
                kind, activity_id(ADAPTER, edit["id"]), parse_timestamp(edit["editedAt"]),
                self._actor(edit["editor"]),
            )
            for edit in node["userContentEdits"]["nodes"]
        ]


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_url(repo: str, number: int, ref: str) -> str | None:
    url = f"https://github.com/{repo}/issues/{number}"
    return url if safe_url(url, ref) == url else None


def _connection_truncated(connection: Mapping[str, Any]) -> bool:
    return bool(
        connection["pageInfo"]["hasNextPage"]
        or connection["totalCount"] > len(connection["nodes"])
    )


def _edits_truncated(node: Mapping[str, Any]) -> bool:
    """Whether ``node``'s edit history may be incomplete or is unavailable."""

    edits = node["userContentEdits"]
    if _connection_truncated(edits):
        return True
    return node["lastEditedAt"] is not None and edits["totalCount"] == 0


def _failure(status: int) -> ActionResult:
    if status in (401, 403):
        return ActionResult("unavailable", "permission_denied")
    return ActionResult("unavailable", "api_error")
