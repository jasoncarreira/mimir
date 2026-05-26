"""Pollers framework — subprocess-shaped external-state watchers.

chainlink #3. Pollers live alongside skills under
``<home>/skills/<name>/`` with a ``pollers.json`` manifest that
declares one or more poller scripts. The scheduler discovers them at
startup (and on the ``mcp__mimir__reload_pollers`` MCP tool call), runs
each on its declared cron, and parses the script's stdout as JSONL.
Each emitted line becomes an ``AgentEvent`` that wakes mimir on a
known channel, exactly like an inbound bridge message.

Why a separate framework from ``register_callable``: in-process
callables are mimir-internal maintenance (saga consolidation, OAuth
quota poll, etc.) — they mutate in-memory state and run regardless
of whether anything changed. Pollers are user-facing watch jobs
that emit events when external state changes; they're isolated as
subprocesses (any language, no mimir import path coupling) and
silence-on-no-change is the filter. New pollers ship as a skill
directory drop, no mimir release required.

Ported from open-strix's ``open_strix.scheduler._discover_pollers`` /
``_on_poller_fire`` (2026-04 vintage).

Output contract (matches open-strix):
- **stdout**: JSONL, one record per actionable event OR algedonic
  signal. Two record shapes share the channel:

  *Event records* — ``{"poller": str, "prompt": str, ...extras}``.
  Each becomes one ``AgentEvent``. Other keys flow through to the
  event's ``extra``.

  *Signal records* — ``{"poller": str, "signal": "<event_type>",
  ...extras}``. These DO NOT spawn an AgentEvent; instead the
  framework writes them to ``events.jsonl`` via ``log_event``, where
  ``feedback._EVENT_RULES`` classifies recognized signal types into
  the algedonic block of the next turn's prompt. Used for
  external-state health signals (auth-token expiry, upstream
  service outages, rate-limit hits) that the agent should see but
  that shouldn't each fire a turn of their own.

  Recognized signal event types (algedonic-classified — see
  ``mimir/feedback.py``):
    ``poller_oauth_expired``       — OAuth token expired/revoked
    ``poller_auth_failed``         — Non-OAuth auth failure
    ``poller_service_outage``      — Upstream service unreachable (5xx, DNS, refused)
    ``poller_rate_limited``        — Upstream rate-limit hit
    ``poller_signal``              — Generic / unclassified poller signal

  A record with neither ``prompt`` nor ``signal`` is silently
  dropped. A record with BOTH is treated as a signal-only record
  (the ``prompt`` is ignored; emit a separate record per shape).

- **stderr**: free-form diagnostic output. Captured and emitted as
  a ``poller_stderr`` event for observability; not forwarded to the
  agent.
- **exit 0**: success (zero events is fine — silence means nothing
  to report). **Non-zero**: error, surfaces as ``poller_nonzero_exit``.

Subprocess gets these env vars injected automatically:
- ``STATE_DIR`` — the skill directory (writable, cursor/state files).
- ``POLLER_NAME`` — the poller's name from pollers.json.
- The host process's environment, plus ``env`` overrides from the
  poller's pollers.json entry.

The 60-second timeout is hard-capped; longer-running pollers should
either run faster or restructure as ``async-tasks``-style background
jobs that emit on completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .event_logger import log_event
from .models import AgentEvent

log = logging.getLogger(__name__)

POLLER_TIMEOUT_SECONDS = 60
# Cap stderr text recorded in events.jsonl so a chatty poller doesn't
# blow the algedonic stream's storage budget.
POLLER_STDERR_LOG_CHARS = 2000
# Cap the per-line stdout payload kept in events.jsonl on parse error
# (bad JSON line). Truncated for the same reason.
POLLER_INVALID_LINE_CHARS = 500
# Cap the prompt text on each emitted poller event (~5x typical
# Discord message). Pollers that need to send larger payloads should
# stash to a file and emit a path reference instead — matches the
# ``<send-file>`` directive shape. A buggy poller that emits a 10 MB
# JSON line would otherwise blow the prompt-build cache and burn
# budget on the next turn (Mimir's PR #88 review nit 4).
POLLER_PROMPT_CHARS = 16_000
# Truncated preview of the rejected prompt kept in
# ``poller_event_rejected`` events for back-pressure debugging.
POLLER_REJECTION_PREVIEW_CHARS = 200
# Default ``batch_size`` when a pollers.json entry doesn't specify
# one. ``1`` preserves the per-event-per-turn shape that matches
# open-strix's framework — every emitted JSONL line becomes one
# AgentEvent, the agent runs once per item. Pollers that produce
# bursty events (github-poller during PR-review activity, RSS during
# heavy publish hours) opt into ``batch_size > 1`` to coalesce items
# into fewer turns.
POLLER_BATCH_SIZE_DEFAULT = 1
# Circuit-breaker: after this many consecutive failures the poller is
# suspended for ``POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS``. "Failure"
# means a non-zero exit, timeout, or subprocess launch error — clean
# exits (even with no events emitted) reset the count.
POLLER_CIRCUIT_BREAKER_THRESHOLD = 3
# How long (seconds) to hold the circuit open after tripping.  5 min
# is long enough to avoid a storm-of-bad-runs while short enough not
# to leave a flaky poller dark for too long.
POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS = 300

#: Channel-id prefix for synthetic poller-tick channels. Each registered
#: poller emits events on ``poller:<name>``. Exported so other modules
#: (recent-activity assembly, scheduler-tick routing) can recognize the
#: prefix without duplicating the literal. Sibling of
#: :data:`mimir.scheduler.SCHEDULER_CHANNEL_PREFIX`.
POLLER_CHANNEL_PREFIX = "poller:"

#: Current pollers.json manifest schema version understood by this build.
#: Manifests with ``schema_version`` absent are treated as v1 (backwards
#: compatible). Manifests with a *higher* version emit a warning but are
#: still parsed on a best-effort basis — most field additions are additive
#: and can be ignored safely; breaking changes would require a major bump.
POLLER_MANIFEST_SCHEMA_VERSION = 1


@dataclass
class _CircuitBreakerState:
    """Per-poller failure-run tracker for the circuit-breaker guard.

    ``consecutive_failures`` counts runs that ended in a non-zero exit,
    timeout, or subprocess launch error since the last clean exit.
    ``disabled_until`` is a Unix timestamp; when it is in the future the
    poller is suppressed and ``run_poller`` returns immediately.  Both
    fields reset to their defaults on the first clean run after a trip.
    """

    consecutive_failures: int = 0
    disabled_until: float = 0.0  # 0.0 → not disabled


#: Module-level per-poller circuit-breaker state.  Keyed by
#: :attr:`PollerConfig.name`.  Lives at module scope (not per-Scheduler)
#: so it survives ``reload_pollers`` calls — a poller that was tripped
#: stays tripped even after the manifest is reloaded.
_circuit_breakers: dict[str, _CircuitBreakerState] = {}


def _cb_record_failure(name: str) -> bool:
    """Increment the consecutive-failure counter for *name*.

    Returns ``True`` the first time the count reaches
    ``POLLER_CIRCUIT_BREAKER_THRESHOLD`` (i.e. the circuit just tripped),
    so the caller can emit a ``poller_circuit_tripped`` event exactly once.
    Returns ``False`` on subsequent failures (circuit already open) or when
    the threshold hasn't been reached yet.
    """
    cb = _circuit_breakers.setdefault(name, _CircuitBreakerState())
    cb.consecutive_failures += 1
    if cb.consecutive_failures == POLLER_CIRCUIT_BREAKER_THRESHOLD:
        cb.disabled_until = time.time() + POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS
        return True
    return False


def _cb_record_success(name: str) -> None:
    """Reset the circuit-breaker state for *name* after a clean run."""
    if name in _circuit_breakers:
        state = _circuit_breakers[name]
        state.consecutive_failures = 0
        state.disabled_until = 0.0


@dataclass
class PollerConfig:
    """One poller declared in a skill's ``pollers.json``.

    ``skill_dir`` is the absolute path to the skill directory; the
    subprocess runs with that as its cwd. ``persist_dir`` is the
    poller's writable cursor/state location — under
    ``<home>/state/pollers/<name>/`` when discovered via
    ``Scheduler.add_poller_jobs`` (filing-rules-aligned, on the
    persistent volume regardless of how the skill itself was
    installed). Tests that construct PollerConfig directly may set
    ``persist_dir == skill_dir`` for compactness.

    ``env`` are extra env vars from the json entry's ``env`` map
    (already coerced to ``dict[str, str]``). Values are literal —
    no shell expansion. Use this when the poller needs a fixed
    config value the operator declared in pollers.json itself.

    ``pass_env`` (chainlink #82 sub #83/#85): explicit list of env
    var names to pass through from the mimir process's environment
    to the subprocess, **bypassing the deny-suffix/deny-prefix
    filter**. This is the supported path for getting secrets
    (``GITHUB_TOKEN``, ``ANTHROPIC_API_KEY``, etc.) and
    ``MIMIR_*``-prefixed knobs (``MIMIR_GITHUB_SELF_LOGIN``) into a
    poller subprocess — the global allowlist
    (``MIMIR_POLLER_ENV_ALLOWLIST``) does NOT bypass the deny
    filter, so it can't be used for ``*_TOKEN`` keys. Each name in
    ``pass_env`` is opt-in per pollers.json entry, so a poller
    declares exactly what it needs and operator-review of the
    manifest is sufficient to audit the trust boundary. Keys named
    in ``pass_env`` that aren't set in the process env are silently
    skipped (no error — that's the operator's signal to set the
    env var); keys named in ``pass_env`` whose names match a
    deny-list pattern emit a ``poller_env_passthrough_named_secret``
    event for visibility (not blocking — it's how operators get
    secrets through).

    ``batch_size`` (chainlink: framework-level coalescing): how
    many emitted JSONL items to bundle into one AgentEvent (= one
    turn the agent sees). ``1`` (default) preserves the per-item-
    per-turn shape that matches open-strix. ``>1`` makes the
    framework collect items from one cron tick and emit
    ``ceil(N/batch_size)`` AgentEvents, each carrying a rendered
    summary of up to ``batch_size`` items + per-item metadata in
    ``extra.items``. Pollers with bursty output (github-poller
    during PR-review storms, RSS during heavy publish hours) set
    this to ``5`` or so to keep the agent's turn count bounded
    without losing the per-item information."""

    name: str
    command: str
    cron: str
    env: dict[str, str]
    skill_dir: Path
    persist_dir: Path | None = None
    batch_size: int = POLLER_BATCH_SIZE_DEFAULT
    pass_env: tuple[str, ...] = ()
    #: Absolute path to the ``pollers.json`` manifest this config was
    #: parsed from, as yielded by ``Path.rglob("pollers.json")`` over
    #: ``skills_dir``. Not explicitly symlink-resolved — two manifests
    #: reachable via different symlink chains would conflate (PR #141
    #: review item #4). Given the ``skills_dir/<skill>/pollers.json``
    #: layout this isn't a realistic concern; if a deployment exposes
    #: ``skills_dir`` as a symlink farm, normalize with ``.resolve()``
    #: at the assignment site in ``discover_pollers``. ``None`` only
    #: when ``PollerConfig`` is constructed directly (most tests).
    #: Used by the scheduler to identify previously-installed entries
    #: whose manifest fails to parse on reload and preserve them
    #: in-place (chainlink #84) — without this back-reference the
    #: scheduler can't tell "manifest deleted on purpose" apart from
    #: "manifest typo'd mid-edit", and the latter silently drops a
    #: working poller.
    manifest_path: Path | None = None

    def channel_id(self) -> str:
        """Synthetic channel for emitted events. Mirrors the
        ``scheduler:<name>`` convention used for null-channel
        scheduler.yaml jobs — keeps poller events queue-isolated
        per-poller (parallel across pollers, serialized within)."""
        return f"{POLLER_CHANNEL_PREFIX}{self.name}"

    def resolved_persist_dir(self) -> Path:
        """Effective persist dir — falls back to skill_dir when not
        explicitly set (compactness for tests + niche call sites)."""
        return self.persist_dir if self.persist_dir is not None else self.skill_dir


def discover_pollers(
    skills_dir: Path,
    *,
    state_root: Path | None = None,
    invalid_manifests: list[tuple[Path, str]] | None = None,
) -> list[PollerConfig]:
    """Walk ``skills_dir/**/pollers.json`` and parse out poller configs.

    Sync — called from the Scheduler at startup before the event loop
    spins up. Per-file failures (bad JSON, missing required fields)
    log a stderr-visible warning but don't abort the walk; one bad
    skill shouldn't take the whole framework down. Returns an empty
    list when ``skills_dir`` doesn't exist (most installs).

    ``state_root`` (when set, typically ``<home>/state/pollers``) is
    where each poller's persistent cursor/state files belong. The
    framework injects ``state_root/<poller_name>`` as ``STATE_DIR``
    in the subprocess env so cursors survive container rebuilds even
    when the skill itself ships in the image. ``state_root=None``
    falls back to ``skill_dir`` (back-compat for tests + skill setups
    where the skill directory itself is on a persistent volume).

    ``invalid_manifests`` (chainlink #84): out-parameter list. When
    provided, each ``pollers.json`` whose **JSON parse** failed (the
    "operator typo'd mid-edit" case) is appended as a
    ``(manifest_path, error_message)`` tuple. The scheduler uses this
    to distinguish "manifest deleted on purpose" from "manifest broke
    mid-edit" on reload and preserve previously-installed pollers in
    the latter case. Format-level failures (top-level type wrong,
    missing 'pollers' key, etc.) are NOT reported here — those are
    structural manifest bugs, not transient typos, and treating them
    as "preserve previously installed" would mask real misconfig.
    Out-list rather than tuple return so existing call sites that
    don't care about the new signal need no unpacking change.
    """
    pollers: list[PollerConfig] = []
    if not skills_dir.exists():
        return pollers

    for pollers_file in sorted(skills_dir.rglob("pollers.json")):
        skill_dir = pollers_file.parent
        try:
            raw = json.loads(pollers_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "poller_invalid_json: %s — %s", pollers_file, exc,
            )
            if invalid_manifests is not None:
                invalid_manifests.append(
                    (pollers_file, f"{type(exc).__name__}: {exc}"),
                )
            continue

        if not isinstance(raw, dict) or "pollers" not in raw:
            log.warning(
                "poller_invalid_format: %s — expected dict with 'pollers' key",
                pollers_file,
            )
            continue

        # Schema-version gate: absent means v1 (backwards compatible).
        # Present-but-unknown → warn and continue best-effort — field
        # additions across minor bumps are typically additive, so the
        # entries we *can* parse are still worth registering.
        schema_version = raw.get("schema_version")
        if schema_version is not None and schema_version != POLLER_MANIFEST_SCHEMA_VERSION:
            log.warning(
                "poller_manifest_unknown_version: %s — got schema_version=%r, "
                "expected %d; attempting best-effort parse",
                pollers_file,
                schema_version,
                POLLER_MANIFEST_SCHEMA_VERSION,
            )

        entries = raw.get("pollers")
        if not isinstance(entries, list):
            log.warning(
                "poller_invalid_format: %s — 'pollers' must be a list",
                pollers_file,
            )
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            command = str(entry.get("command", "")).strip()
            cron = str(entry.get("cron", "")).strip()
            if not name or not command or not cron:
                log.warning(
                    "poller_missing_fields: %s — entry %r",
                    pollers_file, entry,
                )
                continue
            env_raw = entry.get("env", {})
            if not isinstance(env_raw, dict):
                env_raw = {}
            pass_env_raw = entry.get("pass_env", [])
            if not isinstance(pass_env_raw, list):
                log.warning(
                    "poller_invalid_pass_env: %s name=%r value=%r "
                    "(expected list of strings); ignoring",
                    pollers_file, name, pass_env_raw,
                )
                pass_env_raw = []
            pass_env_clean: list[str] = []
            for item in pass_env_raw:
                if not isinstance(item, str):
                    log.warning(
                        "poller_invalid_pass_env_item: %s name=%r "
                        "item=%r (expected string); skipping",
                        pollers_file, name, item,
                    )
                    continue
                key = item.strip()
                if key:
                    pass_env_clean.append(key)
            persist_dir = (
                state_root / name if state_root is not None else None
            )
            # Create the per-poller STATE_DIR at discovery time so
            # operators can drop credentials (`.env`) and cursor seed
            # files into a known location BEFORE the first cron tick
            # fires. ``run_poller`` also mkdir's defensively at run
            # time, but doing it here too means the dir is visible to
            # ``ls`` immediately after ``reload_pollers`` / container
            # boot — the operator doesn't have to wait for the cron
            # schedule to mature.
            if persist_dir is not None:
                try:
                    persist_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log.warning(
                        "poller_persist_dir_create_failed: %s name=%r "
                        "persist_dir=%s — %s",
                        pollers_file, name, persist_dir, exc,
                    )
            # ``batch_size`` is optional; defaults to per-item-per-turn
            # to preserve the open-strix-equivalent shape. Garbage
            # values (negative, non-int, zero) fall back to default
            # with a stderr-visible warning so a typo doesn't silently
            # break batching for a skill the operator just installed.
            #
            # ``log.warning`` (stdlib) rather than ``log_event``
            # (events.jsonl) because ``discover_pollers`` is sync
            # and runs at startup before the asyncio loop spins up;
            # ``log_event`` is async and would deadlock here. Operator
            # scanning events.jsonl for poller config issues won't
            # see this — check container stderr / docker logs instead.
            batch_size = POLLER_BATCH_SIZE_DEFAULT
            raw_batch = entry.get("batch_size", POLLER_BATCH_SIZE_DEFAULT)
            try:
                cand = int(raw_batch)
                if cand >= 1:
                    batch_size = cand
                else:
                    log.warning(
                        "poller_invalid_batch_size: %s name=%r value=%r "
                        "(expected positive int); using default %d",
                        pollers_file, name, raw_batch,
                        POLLER_BATCH_SIZE_DEFAULT,
                    )
            except (TypeError, ValueError):
                log.warning(
                    "poller_invalid_batch_size: %s name=%r value=%r "
                    "(expected positive int); using default %d",
                    pollers_file, name, raw_batch,
                    POLLER_BATCH_SIZE_DEFAULT,
                )
            pollers.append(
                PollerConfig(
                    name=name,
                    command=command,
                    cron=cron,
                    env={str(k): str(v) for k, v in env_raw.items()},
                    skill_dir=skill_dir,
                    persist_dir=persist_dir,
                    batch_size=batch_size,
                    pass_env=tuple(pass_env_clean),
                    manifest_path=pollers_file,
                ),
            )
    return pollers


async def run_poller(
    poller: PollerConfig,
    *,
    enqueue: Callable[[AgentEvent], Awaitable[bool]],
    timeout: float = POLLER_TIMEOUT_SECONDS,
) -> int:
    """Run one poller subprocess; parse its stdout JSONL; enqueue
    each emitted event. Returns the count of events successfully
    enqueued (excludes dispatcher-rejected events; those land in
    ``poller_event_rejected`` events for back-pressure auditing).
    Returns 0 on timeout / error / silence.

    **Command parsing**: ``poller.command`` is parsed by ``/bin/sh -c``
    via ``asyncio.create_subprocess_shell``. Shell features (env-var
    expansion, pipes, redirection) are available — and skill authors
    are responsible for quoting args that contain whitespace or shell
    metacharacters (``"python poller.py 'arg with spaces'"`` —
    NOT ``"python poller.py arg with spaces"``). For arg-handling
    safety, prefer compiling the script to a single binary or quoting
    consistently in ``pollers.json``.

    **Subprocess hygiene**: a ``finally`` block ensures the process is
    killed and reaped on every exit path (timeout, exception, normal
    completion), so cancellation mid-run doesn't leak orphan
    subprocesses or kernel-side zombies on long-lived mimir processes.

    Always logs a ``poller_complete`` event at the end so the operator
    can audit "did the poll cycle run?" even when nothing was emitted.
    The complete event carries both ``events_emitted`` (successful
    enqueues) and ``events_rejected`` (dispatcher said no, indicating
    queue back-pressure) so a mismatch between them is grep-able.
    """
    # --- Circuit-breaker guard -------------------------------------------
    # Check whether this poller is currently suspended due to consecutive
    # failures.  When the circuit is open we skip the subprocess entirely
    # and emit a ``poller_circuit_open`` event so events.jsonl shows the
    # suppression (operator can see "poller X is tripped" without needing
    # to wonder why it went silent).
    _cb = _circuit_breakers.setdefault(poller.name, _CircuitBreakerState())
    _now = time.time()
    if _cb.disabled_until > _now:
        _remaining = int(_cb.disabled_until - _now)
        await log_event(
            "poller_circuit_open",
            poller=poller.name,
            remaining_seconds=_remaining,
            consecutive_failures=_cb.consecutive_failures,
        )
        return 0
    # --- End circuit-breaker guard ----------------------------------------

    persist_dir = poller.resolved_persist_dir()
    # Lazy-create the persist dir on first use. Skill authors who
    # write a cursor file to STATE_DIR can rely on the dir existing.
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        await log_event(
            "poller_persist_dir_create_failed",
            poller=poller.name,
            persist_dir=str(persist_dir),
            error=f"{type(exc).__name__}: {exc}",
        )
        # Fall through; the subprocess might handle a missing dir,
        # OR fail and surface as poller_nonzero_exit.

    # CR2 (external I/O) fix: previously this passed ``{**os.environ,
    # **poller.env}`` — the entire mimir process env (including
    # MIMIR_API_KEY, SAGA_API_KEY, ANTHROPIC_API_KEY, DISCORD_TOKEN,
    # SLACK_BOT_TOKEN, GITHUB_TOKEN, etc.) flowed to every poller
    # subprocess. A buggy poller that printed ``env`` to stderr would
    # leak every secret to events.jsonl (truncated to 2000 chars but
    # still). Skill authors don't expect their poller scripts to
    # inherit these, and we shouldn't expand the trust boundary
    # unnecessarily.
    #
    # Allowlist: shell/locale basics, XDG paths, CA bundles, TMPDIR.
    # PR #111 review widening — initial allowlist was too narrow and
    # would break common pollers (``gh`` users with custom XDG,
    # custom-CA containers, noexec-/tmp setups).
    #
    # Skill authors who need an additional env key declare it in
    # ``pollers.json``'s ``env`` block (per-skill operator-review
    # surface) OR the operator can extend the global allowlist via
    # ``MIMIR_POLLER_ENV_ALLOWLIST`` (comma-separated keys).
    #
    # Hard-deny suffixes / prefixes apply REGARDLESS of operator
    # allowlist additions: ``*_API_KEY``, ``*_TOKEN``, ``*_SECRET``,
    # ``*_PASSWORD`` and ``MIMIR_*`` never reach a poller subprocess.
    _BUILTIN_ALLOWLIST = {
        # Shell + locale
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TZ",
        "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
        "LC_COLLATE", "LC_NUMERIC", "LC_TIME",
        # XDG basedirs (gh + other CLIs respect XDG_CONFIG_HOME)
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
        # Temp-dir overrides for noexec-/tmp setups
        "TMPDIR", "TMP", "TEMP",
        # CA bundles for custom-cert containers
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        # Python
        "PYTHONUNBUFFERED",
        # Terminal
        "TERM", "COLUMNS", "LINES",
    }
    extra_keys = {
        k.strip()
        for k in os.environ.get(
            "MIMIR_POLLER_ENV_ALLOWLIST", "",
        ).split(",")
        if k.strip()
    }
    _allowed = _BUILTIN_ALLOWLIST | extra_keys
    _DENY_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
    _DENY_PREFIXES = ("MIMIR_",)

    def _allowed_env_key(k: str) -> bool:
        if k not in _allowed:
            return False
        if any(k.endswith(s) for s in _DENY_SUFFIXES):
            return False
        if any(k.startswith(p) for p in _DENY_PREFIXES):
            return False
        return True

    env = {k: v for k, v in os.environ.items() if _allowed_env_key(k)}
    # Per-poller pass_env (chainlink #82 sub #83/#85): explicit
    # whitelist of env keys that bypass the deny-suffix/deny-prefix
    # filter AND the built-in allowlist. This is how pollers get
    # secrets (``GITHUB_TOKEN``) and ``MIMIR_*``-prefixed knobs
    # (``MIMIR_GITHUB_SELF_LOGIN``) — the global
    # ``MIMIR_POLLER_ENV_ALLOWLIST`` does NOT bypass the deny filter,
    # so it can't be used for any ``*_TOKEN`` key. It's also the path
    # for arbitrary non-secret keys not in ``_BUILTIN_ALLOWLIST``
    # (e.g. ``GITHUB_REPOS``): pass_env's job is "give the poller these
    # keys regardless of deny / allowlist gating," not specifically
    # "bypass the deny filter."
    #
    # **Precedence**: a key in ``pass_env`` that's ALSO in the
    # allowlist-filtered env unconditionally replaces the allowlist
    # value here (``env[key] = os.environ[key]``). No built-in
    # allowlist key currently matches a typical ``pass_env`` shape,
    # but operators re-declaring ``PATH`` / ``HOME`` etc. in
    # ``pass_env`` for emphasis will see their ``os.environ`` value
    # take precedence over whatever the allowlist filter selected.
    # The ``env`` overlay (``poller.env``) below wins over both —
    # that's the explicit literal-value path and the highest
    # precedence by design.
    #
    # Keys named in pass_env whose names match a deny pattern emit a
    # ``poller_env_passthrough_named_secret`` event for visibility
    # (operators get a paper trail of "this poller pulls a secret
    # named env var through"); the value is not logged. Keys named
    # here that aren't set in os.environ are silently skipped — that
    # absence is itself the operator's signal that the env var wasn't
    # provisioned.
    for key in poller.pass_env:
        if key not in os.environ:
            continue
        env[key] = os.environ[key]
        if any(key.endswith(s) for s in _DENY_SUFFIXES) or any(
            key.startswith(p) for p in _DENY_PREFIXES
        ):
            await log_event(
                "poller_env_passthrough_named_secret",
                poller=poller.name,
                key=key,
            )
    env.update(poller.env)  # explicit per-skill overlay still wins
    env["STATE_DIR"] = str(persist_dir)
    env["POLLER_NAME"] = poller.name

    proc: asyncio.subprocess.Process | None = None
    stdout_bytes = b""
    stderr_bytes = b""
    fatal_error: str | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_shell(
                poller.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(poller.skill_dir),
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await log_event(
                "poller_timeout",
                poller=poller.name,
                timeout_seconds=int(timeout),
            )
            if _cb_record_failure(poller.name):
                await log_event(
                    "poller_circuit_tripped",
                    poller=poller.name,
                    consecutive_failures=_circuit_breakers[poller.name].consecutive_failures,
                    backoff_seconds=POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS,
                    reason="timeout",
                )
            return 0
        except Exception as exc:  # noqa: BLE001 — never let a poller break the scheduler
            fatal_error = f"{type(exc).__name__}: {exc}"
            await log_event(
                "poller_exec_error",
                poller=poller.name,
                error=fatal_error,
            )
            if _cb_record_failure(poller.name):
                await log_event(
                    "poller_circuit_tripped",
                    poller=poller.name,
                    consecutive_failures=_circuit_breakers[poller.name].consecutive_failures,
                    backoff_seconds=POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS,
                    reason="exec_error",
                )
            return 0
    finally:
        # Kill + reap on every exit path. The ``returncode is None``
        # gate makes this a no-op on the happy path (process already
        # exited via ``communicate``); on timeout or exception it
        # ensures we don't leak orphans / zombies.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except (ProcessLookupError, asyncio.CancelledError):
                pass

    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    if stderr_text:
        # Include the subprocess exit code so downstream readers can
        # distinguish diagnostic progress noise (exit_code=0, e.g. ``gh``
        # writing auth progress to stderr on a successful run) from actual
        # errors (exit_code != 0). Without this field every stderr emission
        # looks like a failure in events.jsonl — chainlink #93.
        await log_event(
            "poller_stderr",
            poller=poller.name,
            stderr=stderr_text[:POLLER_STDERR_LOG_CHARS],
            exit_code=proc.returncode if proc is not None else None,
        )

    if proc is None or proc.returncode != 0:
        await log_event(
            "poller_nonzero_exit",
            poller=poller.name,
            returncode=proc.returncode if proc is not None else None,
        )
        if _cb_record_failure(poller.name):
            await log_event(
                "poller_circuit_tripped",
                poller=poller.name,
                consecutive_failures=_circuit_breakers[poller.name].consecutive_failures,
                backoff_seconds=POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS,
                reason="nonzero_exit",
            )
        return 0

    # Clean exit (returncode == 0) — reset the circuit breaker.  This
    # covers both the "has events" and "silent / no events" outcomes;
    # both mean the subprocess ran to successful completion.
    _cb_record_success(poller.name)

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()

    # Phase 1: parse + clean every JSONL line into a list of items.
    # Each item is ``{"prompt": str, "extras": dict[str, Any]}`` —
    # extras are the original parsed keys minus ``prompt``/``poller``.
    # When ``batch_size > 1`` a per-item soft cap also fires here so
    # one chatty item can't starve others by consuming the whole
    # batch-level prompt budget. Single-item batches (default) skip
    # the per-item cap to preserve verbatim pass-through; the
    # batch-level cap below handles single giant prompts.
    items: list[dict[str, Any]] = []
    # Per-item soft cap: divide the prompt budget across the batch,
    # reserving a small slice (50 chars) per item for the numbered
    # marker + newline overhead in the rendered batch. Floors at 100
    # chars to keep tiny batch_sizes from collapsing items to nothing.
    if poller.batch_size > 1:
        per_item_cap: int | None = max(
            100,
            (POLLER_PROMPT_CHARS // poller.batch_size) - 50,
        )
    else:
        per_item_cap = None
    signals_emitted = 0
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            await log_event(
                "poller_invalid_line",
                poller=poller.name,
                line=line[:POLLER_INVALID_LINE_CHARS],
            )
            continue

        if not isinstance(parsed, dict):
            continue

        # Signal-shaped record? Route to events.jsonl via log_event
        # instead of building an AgentEvent. The ``signal`` value is
        # the event_type; ``feedback._EVENT_RULES`` decides whether
        # it surfaces algedonically. Unknown signal types still land
        # in events.jsonl (grep-able for operator debugging) but
        # don't enter the algedonic block.
        #
        # Strip-list rationale:
        #   ``signal``      — discriminator, becomes the event_type arg
        #   ``poller``      — re-stamped explicitly below
        #   ``prompt``      — defensive: a record carrying both
        #                     ``signal`` and ``prompt`` routes as
        #                     signal-only; the ``prompt`` would be
        #                     misleading in the payload
        #   ``event_type``  — collides with ``log_event``'s positional
        #                     parameter; without stripping, a payload
        #                     key called ``event_type`` (e.g. a poller
        #                     surfacing ``"event_type": "invalid_grant"``)
        #                     would raise TypeError on the **payload
        #                     expansion and the signal would drop
        #                     silently. Mimir PR #235 nit.
        signal_type = parsed.get("signal")
        if isinstance(signal_type, str) and signal_type.strip():
            payload = {
                k: v for k, v in parsed.items()
                if k not in ("signal", "poller", "prompt", "event_type")
            }
            try:
                await log_event(
                    signal_type.strip(),
                    poller=poller.name,
                    **payload,
                )
                signals_emitted += 1
            except Exception as exc:  # noqa: BLE001
                # log_event should be best-effort but defend against
                # an unexpected payload shape (non-string keys, etc.)
                # tripping the JSONL writer.
                log.warning(
                    "poller %r: signal emit failed (%s); record dropped",
                    poller.name, exc,
                )
            continue

        prompt = str(parsed.get("prompt", "")).strip()
        if not prompt:
            continue

        # Per-item cap: only when batch_size > 1. Prevents one runaway
        # item from starving others in a batched render. Marks the
        # truncation as ``scope=per_item`` so an operator can tell it
        # apart from the batch-level cap below.
        if per_item_cap is not None and len(prompt) > per_item_cap:
            await log_event(
                "poller_prompt_truncated",
                poller=poller.name,
                original_chars=len(prompt),
                scope="per_item",
            )
            prompt = (
                prompt[:per_item_cap]
                + "\n\n[…truncated by poller framework]"
            )

        # Strip the framework-required keys before stuffing the rest
        # into AgentEvent.extra so downstream prompt rendering can
        # surface platform-specific metadata (source_platform, urls,
        # etc.) without colliding with the AgentEvent dataclass shape.
        extras = {
            k: v for k, v in parsed.items()
            if k not in ("prompt", "poller")
        }
        items.append({"prompt": prompt, "extras": extras})

    if not items:
        await log_event(
            "poller_complete",
            poller=poller.name,
            events_emitted=0,
            events_rejected=0,
            items_collected=0,
            batches_emitted=0,
            signals_emitted=signals_emitted,
        )
        return 0

    # Phase 2: batch items into groups of up to ``poller.batch_size``.
    # batch_size=1 preserves the per-item-per-turn shape; >1 coalesces
    # to ``ceil(len(items) / batch_size)`` AgentEvents.
    # ``max(1, ...)`` is defense-in-depth: ``discover_pollers``
    # already filters non-positive values, but tests construct
    # ``PollerConfig`` directly bypassing that path.
    batch_size = max(1, poller.batch_size)
    batches: list[list[dict[str, Any]]] = [
        items[i:i + batch_size]
        for i in range(0, len(items), batch_size)
    ]
    # Per-fire timestamp scoped to source_id — disambiguates events
    # across overlapping fires (manual fire racing a scheduled fire,
    # two scheduled ticks delivered out-of-order). ms granularity is
    # enough for any realistic fire cadence; no risk of collision in
    # practice.
    fire_ts_ms = int(time.time() * 1000)

    # Phase 3: assemble + dispatch each batch as one AgentEvent.
    event_count = 0
    rejected_count = 0
    for batch_idx, batch in enumerate(batches):
        content = _render_batch(poller.name, batch, batch_idx, len(batches))
        # Apply the prompt cap once more on the assembled batch — even
        # with per-item caps, ``batch_size × cap`` could exceed the
        # prompt-build budget on a worst-case fire.
        if len(content) > POLLER_PROMPT_CHARS:
            await log_event(
                "poller_prompt_truncated",
                poller=poller.name,
                original_chars=len(content),
                batch_index=batch_idx,
                scope="batch",
            )
            content = (
                content[:POLLER_PROMPT_CHARS]
                + "\n\n[…truncated by poller framework]"
            )

        # Per-batch extra. ``items`` carries per-item metadata so the
        # agent can react to specific items without re-parsing the
        # rendered prompt. ``batch_index`` / ``batch_count`` let
        # multi-batch fires identify "this is part 2 of 3" without
        # the agent having to read the prompt header.
        extra: dict[str, Any] = {
            "poller_name": poller.name,
            "batch_index": batch_idx,
            "batch_count": len(batches),
            "items": [item["extras"] for item in batch],
        }
        event = AgentEvent(
            trigger="poller",
            channel_id=poller.channel_id(),
            content=content,
            source="poller",
            source_id=f"{POLLER_CHANNEL_PREFIX}{poller.name}:{fire_ts_ms}:batch:{batch_idx}",
            extra=extra,
        )
        try:
            accepted = await enqueue(event)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "poller_enqueue_error",
                poller=poller.name,
                error=f"{type(exc).__name__}: {exc}",
                batch_index=batch_idx,
            )
            continue
        if accepted:
            event_count += 1
        else:
            rejected_count += 1
            # Back-pressure observability: when the dispatcher refuses
            # an event (queue cap hit, channel saturated, etc.) record
            # it so the events_emitted vs events_rejected gap on
            # poller_complete is grep-able. Truncated preview keeps
            # events.jsonl small for chatty pollers.
            await log_event(
                "poller_event_rejected",
                poller=poller.name,
                prompt_preview=content[:POLLER_REJECTION_PREVIEW_CHARS],
                batch_index=batch_idx,
            )

    # ``events_emitted`` counts AgentEvents enqueued (= batches that
    # the dispatcher accepted). Pre-batching it counted parsed JSONL
    # lines, which was 1:1 with items. With ``batch_size > 1`` the
    # two diverge: ``items_collected`` is the per-item count (what
    # the old field meant) and ``events_emitted`` is now the per-
    # turn count (= batches enqueued, what the agent actually sees).
    # Operator queries reading ``events_emitted`` for "how many items
    # came in?" should switch to ``items_collected``; queries asking
    # "how many turns will this fire?" stay on ``events_emitted``.
    await log_event(
        "poller_complete",
        poller=poller.name,
        events_emitted=event_count,
        events_rejected=rejected_count,
        items_collected=len(items),
        batches_emitted=len(batches),
        signals_emitted=signals_emitted,
    )
    return event_count


def _render_batch(
    poller_name: str,
    batch: list[dict[str, Any]],
    batch_index: int,
    batch_count: int,
) -> str:
    """Render a batch of items into the prompt body for one
    ``AgentEvent``. Single-item batches return the item's prompt
    verbatim — preserves the open-strix-equivalent rendering for
    pollers with ``batch_size=1`` (default). Multi-item batches
    render with a header (``poller-name reported N items``) and a
    numbered list of per-item prompts so the agent can scan + react
    to specific items.

    Multi-batch fires (when ``batch_count > 1``) include a "batch X
    of Y" suffix on the header so the agent can tell the rest of the
    items are coming on subsequent turns and decide whether to wait
    or act on each batch independently.
    """
    if len(batch) == 1:
        return batch[0]["prompt"]
    header = f"{poller_name} reported {len(batch)} items"
    if batch_count > 1:
        header += f" (batch {batch_index + 1} of {batch_count})"
    header += ":"
    body_lines = [header]
    # Width of the largest marker — left-pad smaller numbers so
    # ``" 1. "`` and ``"10. "`` align in batches that cross 10 items.
    # Continuation indent matches the marker width + 2 (period +
    # space) so multi-line item prompts stay visually grouped under
    # their parent.
    width = len(str(len(batch)))
    cont_indent = " " * (width + 2)
    for i, item in enumerate(batch, start=1):
        item_lines = item["prompt"].splitlines() or [""]
        body_lines.append(f"{i:>{width}}. {item_lines[0]}")
        for tail in item_lines[1:]:
            body_lines.append(f"{cont_indent}{tail}")
    return "\n".join(body_lines)


__all__ = (
    "PollerConfig",
    "discover_pollers",
    "run_poller",
    "POLLER_TIMEOUT_SECONDS",
    "POLLER_PROMPT_CHARS",
    "POLLER_CIRCUIT_BREAKER_THRESHOLD",
    "POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS",
    "_circuit_breakers",  # exposed for tests + introspection; prefixed as internal
)
