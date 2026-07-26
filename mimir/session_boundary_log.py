"""Session-summary rendering for the prompt-assembly path.

The agent's prompt builder needs a ``## Recent session summaries`` block
showing the last few session boundaries on the current channel.
Boundaries come from ``SagaStore.recent_session_boundaries()``; this
module owns the *rendering* (chainlink #63 staleness markers,
closed_since corrective filtering) and the ``count_turns_since()``
helper that annotates each header with a "N turns this channel" marker.

History: this module used to also own a local JSONL mirror
(``SessionBoundaryLog``) that the legacy ``saga_end_session`` tool
populated as a fallback for the prompt path when an external SAGA
HTTP server was briefly unreachable. mimir.saga is now in-process —
SagaStore is always available — so the mirror was orphaned (write
path dead, read path always empty) and was removed.

chainlink #63: session-summary Unfinished lists are point-in-time
snapshots that go stale fast. The renderer annotates each summary
header with relative-age + turn-count markers, suffixes the Unfinished
sub-bullet with ``[verify before quoting]`` past either staleness
threshold, and applies ``closed_since`` corrective overrides written
by later boundaries (drops resolved items via case-insensitive
substring match against the closed_since refs).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ._jsonl_tail import tail_jsonl_records

log = logging.getLogger(__name__)


# Minimum length for a closed_since entry to participate in substring
# matching. Single-character refs would over-match (e.g. "#" appearing
# in any prose); two-character is the natural floor for things like
# "#1" or short PR refs while still rejecting empty/single-char noise.
_MIN_CLOSED_SINCE_REF_LEN = 2

# A turn can carry several old boundaries, each with several artifacts. Keep
# validation to one small GraphQL request rather than letting prompt assembly
# grow with the size of SAGA history.
MAX_UNFINISHED_LOOKUPS = 20

_GITHUB_REF_RE = re.compile(
    r"https?://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/(?:"
    r"(?P<pr>pull)|(?P<issue>issues))/(?P<number>\d+)",
    re.IGNORECASE,
)
_PR_REF_RE = re.compile(r"\b(?:PR|pull request)\s*#?(\d+)\b", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"\b(?:issue|chainlink)\s*#?(\d+)\b", re.IGNORECASE)
_LOCAL_PUSH_CLAIM_RE = re.compile(
    r"\b(?:not (?:yet )?pushed|unpushed|locally committed|"
    r"local (?:branch|commit)|but not pushed)\b",
    re.IGNORECASE,
)
_COMMIT_REF_RE = re.compile(
    r"\b(?:commit|head)\s+([0-9a-f]{7,40})\b", re.IGNORECASE,
)
_BRANCH_REF_RE = re.compile(
    r"\bbranch\s+[`'\"]?([A-Za-z0-9._/-]+)", re.IGNORECASE,
)


@dataclass(frozen=True)
class GithubArtifact:
    """A GitHub object named by an unfinished-work item."""

    repo: str
    kind: str
    value: str


ArtifactLookup = Callable[[list[GithubArtifact]], dict[GithubArtifact, str | None]]


def has_validatable_unfinished_work(boundaries: Iterable[dict[str, Any]]) -> bool:
    """Return whether a boundary names an artifact worth a live lookup."""
    return any(
        _extract_artifacts(str(item), default_repo="placeholder/repo")
        for boundary in boundaries
        for item in (boundary.get("unfinished") or [])
    )


def validate_unfinished_work(
    boundaries: list[dict[str, Any]],
    *,
    default_repo: str | None = None,
    lookup: ArtifactLookup | None = None,
    max_lookups: int = MAX_UNFINISHED_LOOKUPS,
) -> list[dict[str, Any]]:
    """Validate artifact claims and return copied, corrected boundaries.

    Definite terminal states remove an item only when every artifact it names
    is terminal. Open artifacts survive verbatim. Ambiguous, unavailable, and
    over-cap results fail open with an explicit ``[unverified]`` marker.
    ``lookup`` exists both for tests and alternate GitHub clients; the default
    implementation performs one batched ``gh api graphql`` request.
    """
    if not boundaries:
        return []
    repo = default_repo or _default_github_repo()
    item_artifacts: list[tuple[int, int, list[GithubArtifact]]] = []
    unique: list[GithubArtifact] = []
    seen: set[GithubArtifact] = set()
    copied = [dict(boundary) for boundary in boundaries]

    for boundary_index, boundary in enumerate(copied):
        unfinished = list(boundary.get("unfinished") or [])
        boundary["unfinished"] = unfinished
        for item_index, item in enumerate(unfinished):
            artifacts = _extract_artifacts(str(item), default_repo=repo)
            if not artifacts:
                continue
            item_artifacts.append((boundary_index, item_index, artifacts))
            for artifact in artifacts:
                if artifact.repo and artifact not in seen:
                    seen.add(artifact)
                    unique.append(artifact)

    selected = unique[:max(0, max_lookups)]
    statuses: dict[GithubArtifact, str | None] = {}
    if selected:
        try:
            statuses = (lookup or _lookup_github_artifacts)(selected)
        except Exception:  # noqa: BLE001 - prompt assembly must fail open
            log.exception("unfinished-work GitHub validation failed")

    drops: dict[int, set[int]] = {}
    for boundary_index, item_index, artifacts in item_artifacts:
        values = [statuses.get(artifact) for artifact in artifacts]
        if _item_is_terminal(artifacts, values):
            drops.setdefault(boundary_index, set()).add(item_index)
            log.debug(
                "session_summary_unfinished_validated: dropped %r (%r)",
                copied[boundary_index]["unfinished"][item_index], values,
            )
        elif any(status is None for status in values):
            text = str(copied[boundary_index]["unfinished"][item_index]).strip()
            if "[unverified]" not in text.lower():
                copied[boundary_index]["unfinished"][item_index] = (
                    f"{text} [unverified]"
                )

    for boundary_index, indexes in drops.items():
        copied[boundary_index]["unfinished"] = [
            item for index, item in enumerate(copied[boundary_index]["unfinished"])
            if index not in indexes
        ]
    return copied


def _extract_artifacts(text: str, default_repo: str | None) -> list[GithubArtifact]:
    artifacts: list[GithubArtifact] = []
    url_spans: list[tuple[int, int]] = []
    for match in _GITHUB_REF_RE.finditer(text):
        kind = "pr" if match.group("pr") else "issue"
        artifacts.append(GithubArtifact(
            match.group("repo").removesuffix(".git"), kind, match.group("number"),
        ))
        url_spans.append(match.span())

    def outside_url(match: re.Match[str]) -> bool:
        return not any(start <= match.start() < end for start, end in url_spans)

    for regex, kind in ((_PR_REF_RE, "pr"), (_ISSUE_REF_RE, "issue")):
        for match in regex.finditer(text):
            if outside_url(match):
                artifacts.append(GithubArtifact(default_repo or "", kind, match.group(1)))

    # Branches and commits are only terminal evidence for claims that they are
    # still local/unpushed. A generic "work on branch X" remains genuine work
    # even after the branch reaches GitHub.
    if _LOCAL_PUSH_CLAIM_RE.search(text):
        for match in _COMMIT_REF_RE.finditer(text):
            artifacts.append(GithubArtifact(default_repo or "", "commit", match.group(1)))
        for match in _BRANCH_REF_RE.finditer(text):
            artifacts.append(GithubArtifact(default_repo or "", "branch", match.group(1)))
    return list(dict.fromkeys(artifacts))


def _artifact_is_terminal(artifact: GithubArtifact, status: str | None) -> bool:
    normalized = str(status or "").upper()
    if artifact.kind == "pr":
        return normalized in {"CLOSED", "MERGED"}
    if artifact.kind == "issue":
        return normalized == "CLOSED"
    return normalized == "REMOTE"


def _item_is_terminal(
    artifacts: list[GithubArtifact], statuses: list[str | None],
) -> bool:
    """Require every work claim to be resolved, allowing push alternatives."""
    ordinary = [
        (artifact, status) for artifact, status in zip(artifacts, statuses)
        if artifact.kind in {"pr", "issue"}
    ]
    local = [
        status for artifact, status in zip(artifacts, statuses)
        if artifact.kind in {"commit", "branch"}
    ]
    ordinary_done = all(
        _artifact_is_terminal(artifact, status) for artifact, status in ordinary
    )
    # A remote commit OR its branch is conclusive evidence against one
    # "committed but not pushed" claim. The other ref may have been deleted
    # after merge and must not turn that definite evidence into ambiguity.
    local_done = not local or any(str(status or "").upper() == "REMOTE" for status in local)
    return ordinary_done and local_done


def _default_github_repo() -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(
        r"(?:github\.com[/:])([^/\s:]+/[^/\s]+?)(?:\.git)?$",
        result.stdout.strip(),
    )
    return match.group(1) if match else None


def _lookup_github_artifacts(
    artifacts: list[GithubArtifact],
) -> dict[GithubArtifact, str | None]:
    fields: list[str] = []
    for index, artifact in enumerate(artifacts):
        owner, name = artifact.repo.split("/", 1)
        owner_arg, name_arg = json.dumps(owner), json.dumps(name)
        if artifact.kind in {"pr", "issue"}:
            fields.append(
                f"a{index}:repository(owner:{owner_arg},name:{name_arg}){{"
                f"issueOrPullRequest(number:{int(artifact.value)}){{__typename "
                "... on PullRequest{state} ... on Issue{state}}}"
            )
        elif artifact.kind == "commit":
            value_arg = json.dumps(artifact.value)
            fields.append(
                f"a{index}:repository(owner:{owner_arg},name:{name_arg}){{"
                f"object(expression:{value_arg}){{__typename}}}}"
            )
        else:
            ref_arg = json.dumps(f"refs/heads/{artifact.value}")
            fields.append(
                f"a{index}:repository(owner:{owner_arg},name:{name_arg}){{"
                f"ref(qualifiedName:{ref_arg}){{name}}}}"
            )
    query = "query{" + " ".join(fields) + "}"
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    env = {**os.environ, "GH_TOKEN": token} if token else None
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "empty GitHub response")
    payload = json.loads(result.stdout)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub response had no data")

    statuses: dict[GithubArtifact, str | None] = {}
    for index, artifact in enumerate(artifacts):
        repository = data.get(f"a{index}")
        if not isinstance(repository, dict):
            statuses[artifact] = None
            continue
        if artifact.kind in {"pr", "issue"}:
            node = repository.get("issueOrPullRequest")
            expected_type = "PullRequest" if artifact.kind == "pr" else "Issue"
            statuses[artifact] = (
                str(node.get("state"))
                if isinstance(node, dict) and node.get("__typename") == expected_type
                else None
            )
        elif artifact.kind == "commit":
            node = repository.get("object")
            statuses[artifact] = (
                "REMOTE" if isinstance(node, dict)
                and node.get("__typename") == "Commit" else None
            )
        else:
            statuses[artifact] = "REMOTE" if repository.get("ref") else None
    return statuses


def render_session_summaries(
    boundaries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    turn_counts: dict[str, int] | None = None,
    stale_age_hours: int = 2,
    stale_turns: int = 5,
) -> str | None:
    """Markdown body for the ``## Recent session summaries`` block.

    Each entry: ``YYYY-MM-DD HH:MM (~Xh ago, N turns this channel)
    (channel) — <summary>`` plus a one-line ``Unfinished:`` bullet
    when non-empty (suffixed ``[verify before quoting]`` once a
    staleness threshold trips). Stored-but-not-rendered fields
    (topics_discussed, decisions_made, emotional_state) are reachable
    via SAGA semantic retrieval; they'd add noise here.

    chainlink #63 staleness markers:
    - ``now`` is the wall-clock reference for the age suffix. ``None``
      skips age rendering (callers can opt out for tests / niche
      uses).
    - ``turn_counts[boundary_key]`` is the number of turns on the
      same channel since the boundary's ``ts``. Each boundary's key
      is its ``ts`` string (or empty string when ``ts`` is missing).
      Missing keys render as zero turns.
    - ``stale_age_hours`` / ``stale_turns`` thresholds: when *either*
      signal exceeds its threshold, the Unfinished bullet's header
      gets a ``[verify before quoting]`` suffix.
    - Each boundary's ``closed_since`` list (refs of items resolved
      since this boundary) is collected and used to drop stale items
      from *earlier* boundaries' Unfinished lists. Drop is by
      case-insensitive substring match — closed_since refs ≥
      ``_MIN_CLOSED_SINCE_REF_LEN`` characters are treated as
      substrings, and any Unfinished item containing one of them
      is dropped from the rendering.

    Returns ``None`` when the input is empty (or every Unfinished item
    got dropped + every summary is otherwise empty) so the caller can
    skip rendering an empty section. Always renders header lines for
    non-empty input — an empty Unfinished is itself a useful signal.
    """
    if not boundaries:
        return None
    # Per-boundary closed_since application is **asymmetric**: only refs
    # from chronologically-LATER boundaries get applied. Otherwise a
    # T1 closure of "#71" would also drop T2's "#71 reverted, reopened"
    # — collapsing a revert/reopen cycle into invisibility (Mimir's
    # PR #86 review nit). When timestamps are unparseable we apply
    # conservatively (treat all other boundaries as later) — preserves
    # behavior for the rare badly-formed-ts case.
    parsed_timestamps: list[Optional[datetime]] = [
        _parse_iso_ts(str(b.get("ts") or b.get("timestamp") or ""))
        for b in boundaries
    ]
    lines: list[str] = []
    for i, b in enumerate(boundaries):
        # Both shapes accepted: local mirror writes ``ts`` /
        # ``channel_id``; SAGA's get_last_sessions returns
        # ``timestamp`` / ``channel`` (chainlink #63 latent fix). The
        # local-mirror naming wins when both are present.
        ts_raw = str(b.get("ts") or b.get("timestamp") or "")
        ts = _short_ts(ts_raw)
        ch = b.get("channel_id") or b.get("channel") or "-"
        summary = (b.get("summary") or "").strip() or "(no summary)"
        # Single-line summary; collapse internal newlines so the bullet
        # stays compact and readable.
        summary = " ".join(summary.split())

        age_str = _format_relative_age(ts_raw, now)
        # Per-boundary turn count: only render the marker when the
        # caller explicitly supplied a counts mapping. Tests + niche
        # call sites that don't care can omit ``turn_counts`` entirely
        # and get the lean rendering. The agent's prompt builder
        # always passes one (chainlink #63).
        turn_count: int | None
        if turn_counts is None:
            turn_count = None
        else:
            turn_count = turn_counts.get(ts_raw, 0)
        markers: list[str] = []
        if age_str:
            markers.append(age_str)
        if turn_count is not None:
            markers.append(_format_turn_count(turn_count))
        if markers:
            marker_str = ", ".join(markers)
            header_meta = f"({marker_str}) ({ch})"
        else:
            header_meta = f"({ch})"
        if ts:
            lines.append(f"- {ts} {header_meta} — {summary}")
        else:
            lines.append(f"- {header_meta} — {summary}")

        # Apply closed_since drops from chronologically-later boundaries
        # (Mimir's PR #86 nit: avoid collapsing revert/reopen cycles).
        unfinished = b.get("unfinished") or []
        later_refs: list[str] = []
        self_ts = parsed_timestamps[i]
        for j, other in enumerate(boundaries):
            if j == i:
                continue
            other_ts = parsed_timestamps[j]
            # If either side's timestamp is unparseable, apply
            # conservatively — preserves the older symmetric behavior
            # for malformed records, which was the only available
            # signal in that case.
            if self_ts is not None and other_ts is not None and other_ts <= self_ts:
                continue
            for ref in other.get("closed_since") or []:
                ref_str = str(ref).strip()
                if len(ref_str) >= _MIN_CLOSED_SINCE_REF_LEN:
                    later_refs.append(ref_str)
        kept = _apply_closed_since(unfinished, later_refs)
        if kept:
            joined = "; ".join(str(x).strip() for x in kept if str(x).strip())
            if joined:
                # Threshold for the verify-before-quoting suffix:
                # either signal alone trips it. Skip evaluation when
                # neither signal was supplied (tests / lean callers).
                age_hours = _age_hours(ts_raw, now)
                trips_age = (
                    age_hours is not None and age_hours >= stale_age_hours
                )
                trips_turns = (
                    turn_count is not None and turn_count >= stale_turns
                )
                suffix = (
                    " [verify before quoting]"
                    if (trips_age or trips_turns) else ""
                )
                lines.append(f"  Unfinished{suffix}: {joined}")
    return "\n".join(lines)


def _short_ts(ts: str) -> str:
    cleaned = ts.replace("T", " ")
    return cleaned[:16] if len(cleaned) >= 16 else cleaned


def _parse_iso_ts(ts: str) -> Optional[datetime]:
    """Parse a session-boundary timestamp string. Accepts ISO-8601 with
    or without a ``Z`` suffix; returns None when unparseable. Always
    returns a tz-aware datetime (UTC) so subtraction with ``now``
    doesn't raise."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(ts: str, now: datetime | None) -> Optional[float]:
    """Compute age in fractional hours; None when timestamp is
    unparseable or ``now`` wasn't supplied."""
    if now is None:
        return None
    parsed = _parse_iso_ts(ts)
    if parsed is None:
        return None
    delta = now - parsed
    return delta.total_seconds() / 3600.0


def _format_relative_age(ts: str, now: datetime | None) -> Optional[str]:
    """Render age as a compact string. Buckets match feedback.py's
    target_age formatter:
      <1m  → "<1m ago"
      <1h  → "{N}m ago"
      <1d  → "~{N}h ago"
      else → "~{N}d ago"
    Returns None when ``now`` wasn't supplied or ts is unparseable."""
    hours = _age_hours(ts, now)
    if hours is None:
        return None
    minutes = hours * 60.0
    if minutes < 1:
        return "<1m ago"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if hours < 24:
        return f"~{int(hours)}h ago"
    days = hours / 24.0
    return f"~{int(days)}d ago"


def _format_turn_count(n: int) -> str:
    if n == 1:
        return "1 turn this channel"
    return f"{int(n)} turns this channel"


def _apply_closed_since(
    unfinished: Iterable[Any], closed_since_refs: list[str],
) -> list[str]:
    """Drop unfinished items where any closed_since ref appears with
    digit-aware word boundaries (case-insensitive). Refs shorter than
    ``_MIN_CLOSED_SINCE_REF_LEN`` are filtered out.

    The boundary check is digit-only — ``(?<!\\d)<ref>(?!\\d)`` —
    rather than ``\\b...\\b`` because ``#`` and other ref characters
    aren't word-class. Practical effect: ``#1`` matches in
    ``"chainlink #1 something"`` but does NOT match in ``"#10"``,
    ``"#100"``, etc., closing the bug class Mimir flagged on PR #86.
    Letters / spaces / punctuation around a ref are still permitted.

    Drops are logged at DEBUG level so future-mimir debugging
    "why didn't this Unfinished item show up?" has an audit trail.

    Returns a fresh list — does NOT mutate the input."""
    if not closed_since_refs:
        return [str(u) for u in unfinished if str(u).strip()]
    patterns = [
        re.compile(rf"(?<!\d){re.escape(r)}(?!\d)", re.IGNORECASE)
        for r in closed_since_refs
    ]
    kept: list[str] = []
    for item in unfinished:
        text = str(item).strip()
        if not text:
            continue
        match = next(
            (p for p in patterns if p.search(text)), None,
        )
        if match is not None:
            log.debug(
                "session_summary_unfinished_filtered: dropped %r "
                "(matched closed_since ref %r)",
                text, match.pattern,
            )
            continue
        kept.append(text)
    return kept


def _turn_records(
    turns_log_path: Path,
    snapshot_records: Optional[Callable[[], Iterable[dict[str, Any]]]],
) -> Iterable[dict[str, Any]]:
    if snapshot_records is not None:
        return snapshot_records()
    try:
        return tail_jsonl_records(turns_log_path)
    except FileNotFoundError:
        return []


def count_turns_since(
    turns_log_path: Path, channel_id: str, since_ts: str,
    *,
    snapshot_records: Optional[Callable[[], Iterable[dict[str, Any]]]] = None,
) -> int:
    """Count turns on ``channel_id`` with ``ts > since_ts``.

    Used by the prompt builder to annotate each session-summary header
    with a "{N} turns this channel" marker so the agent can tell how
    much work has happened since the boundary was written. Comparison
    is on the records' ISO ``ts`` field as strings (lexicographic
    matches chronological for ISO-8601 with consistent timezone).

    ``snapshot_records`` is the in-memory iterator used by callers
    that hold a JsonlSnapshot; falls back to a tail-stream of
    ``turns_log_path`` when not supplied.

    Returns 0 when the path doesn't exist or ``since_ts`` is empty
    (which would otherwise match every record).
    """
    if not since_ts:
        return 0
    return count_turns_since_many(
        turns_log_path,
        channel_id=channel_id,
        since_timestamps=[since_ts],
        snapshot_records=snapshot_records,
    ).get(since_ts, 0)


def count_turns_since_many(
    turns_log_path: Path,
    channel_id: str,
    since_timestamps: Iterable[str],
    *,
    snapshot_records: Optional[Callable[[], Iterable[dict[str, Any]]]] = None,
) -> dict[str, int]:
    """Count channel turns after each timestamp in one records pass.

    This is the prompt-session-summary helper used by
    ``Agent._assemble_session_summaries``. It is intentionally
    synchronous so the async caller can place the whole JSONL
    snapshot/tail drain and comparison loop behind ``asyncio.to_thread``;
    prompt assembly must not parse ``turns.jsonl`` on the scheduler event
    loop.
    """
    cutoffs = [str(ts) for ts in since_timestamps if str(ts)]
    if not cutoffs:
        return {}
    counts = dict.fromkeys(cutoffs, 0)
    for rec in _turn_records(turns_log_path, snapshot_records):
        rec_ch = rec.get("channel_id")
        if rec_ch != channel_id:
            continue
        rec_ts = rec.get("ts")
        if not rec_ts:
            continue
        rec_ts_s = str(rec_ts)
        for cutoff in cutoffs:
            if rec_ts_s > cutoff:
                counts[cutoff] += 1
    return counts
