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
- ``STATE_DIR`` — the poller's persistent state directory.
- ``POLLER_NAME`` — the poller's name from pollers.json.
- ``MIMIR_HOME`` — the authoritative agent home path from ``Config.home``.
- A scrubbed subset of the host process environment, explicit ``pass_env``
  passthrough keys, plus literal ``env`` overrides from the poller's
  pollers.json entry.

The 60-second timeout is hard-capped; longer-running pollers should
either run faster or restructure as ``async-tasks``-style background
jobs that emit on completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .billing import normalize_priority
from .event_logger import log_event, get_events_path
from .models import AgentEvent
from .redaction import redact_text
from . import poller_recovery

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
# Hard byte ceilings on a poller subprocess's stdout/stderr (chainlink
# #258). The POLLER_PROMPT_CHARS cap above only applies AFTER the bytes
# are read — ``communicate()`` buffers the ENTIRE stream first, so a
# runaway poller writing gigabytes in its timeout window would OOM mimir
# before any char cap engaged. We instead drain incrementally and kill
# the process once cumulative output crosses these ceilings. 8 MB is far
# above any legitimate poller (the prompt cap is 16 KB) while bounding
# worst-case memory; stderr is diagnostic, so a tighter 1 MB.
MAX_POLLER_STDOUT_BYTES = 8 * 1024 * 1024
MAX_POLLER_STDERR_BYTES = 1 * 1024 * 1024
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
# Grace window (seconds) for the subprocess to exit after both pipe
# drains hit EOF (chainlink #410). EOF only proves the child CLOSED
# its stdout/stderr fds — not that it exited. A poller that closes
# both fds and keeps running (daemonizing helper, post-cleanup hang)
# would otherwise pin a bare ``proc.wait()`` — and the caller's
# concurrency-semaphore slot — forever. Capped by the poller's own
# timeout so a short-timeout caller is never held longer than 2x its
# budget.
POLLER_EXIT_GRACE_SECONDS = 5.0

#: Channel-id prefix for synthetic poller-tick channels. Each registered
#: poller emits events on ``poller:<name>``. Exported so other modules
#: (recent-activity assembly, scheduler-tick routing) can recognize the
#: prefix without duplicating the literal. Sibling of
#: :data:`mimir.scheduler.SCHEDULER_CHANNEL_PREFIX`.
POLLER_CHANNEL_PREFIX = "poller:"


_BUILTIN_POLLER_ENV_ALLOWLIST = frozenset({
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
})
_DENY_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_DENY_ENV_PREFIXES = ("MIMIR_",)
# chainlink #229: hard-deny on process-control / loader env vars that can
# hijack the subprocess. Unlike suffix/prefix names (which can pass via
# ``pass_env`` with an audit event), these never pass through.
_PROCESS_CONTROL_ENV_DENY = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FORCE_FLAT_NAMESPACE",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONHOME",
    "PYTHONUSERBASE",
})
_POLLER_INJECTED_ENV_KEYS = frozenset({"STATE_DIR", "POLLER_NAME", "MIMIR_HOME"})


def _extra_poller_env_allowlist() -> set[str]:
    return {
        k.strip()
        for k in os.environ.get("MIMIR_POLLER_ENV_ALLOWLIST", "").split(",")
        if k.strip()
    }


def _allowed_poller_env_key(k: str, *, allowed: set[str] | frozenset[str] | None = None) -> bool:
    if allowed is None:
        allowed = _BUILTIN_POLLER_ENV_ALLOWLIST | _extra_poller_env_allowlist()
    if k not in allowed:
        return False
    if any(k.endswith(s) for s in _DENY_ENV_SUFFIXES):
        return False
    if any(k.startswith(p) for p in _DENY_ENV_PREFIXES):
        return False
    return True


def _poller_env_available_at_discovery(
    *,
    env_raw: dict[str, object],
    pass_env: list[str],
    injected_keys: set[str] | frozenset[str] = _POLLER_INJECTED_ENV_KEYS,
) -> set[str]:
    """Return env keys that will exist in the assembled subprocess env.

    Mirrors ``run_poller`` without values or async logging: scrubbed host env,
    explicit ``pass_env`` passthrough except process-control hard-denies, literal
    manifest ``env`` overrides, and framework-injected names. Discovery uses this
    to decide whether ``env_required`` pollers are schedulable.
    """
    allowed = _BUILTIN_POLLER_ENV_ALLOWLIST | _extra_poller_env_allowlist()
    available = {
        k for k in os.environ
        if _allowed_poller_env_key(k, allowed=allowed)
    }
    available.update(
        k for k in pass_env
        if k not in _PROCESS_CONTROL_ENV_DENY and k in os.environ
    )
    # Manifest ``env`` keys are also process-control-filtered at runtime
    # (chainlink #421) — mirror that here so an ``env_required`` name
    # that the runtime would strip doesn't count as available.
    available.update(
        str(k) for k in env_raw if str(k) not in _PROCESS_CONTROL_ENV_DENY
    )
    available.update(injected_keys)
    return available


def _redact_poller_env_values(text: str, env: dict[str, str]) -> str:
    """Redact exact secret-named subprocess env values from poller diagnostics.

    Token-shaped redaction is global in ``event_logger``, but ``pass_env`` can
    forward secrets with no recognizable shape (bare DB passwords, shared HMACs,
    etc.). Poller stdout/stderr is the place those values are most likely to be
    echoed. Redact exact values for secret-named keys before line/length
    truncation so a long leaked value cannot leave its prefix behind in durable
    events.
    """
    if not text:
        return text
    out = redact_text(text)
    secret_names = {suffix.removeprefix("_") for suffix in _DENY_ENV_SUFFIXES}
    secret_values = {
        value
        for key, value in env.items()
        if any(key.endswith(suffix) for suffix in _DENY_ENV_SUFFIXES)
        or key in secret_names
    }
    # Longest first prevents a short value from partially masking inside a
    # longer one and leaving the suffix visible. Skip tiny values to avoid
    # shredding ordinary diagnostics like PATH separators or boolean flags.
    for value in sorted(secret_values, key=len, reverse=True):
        if len(value) < 6:
            continue
        out = out.replace(value, "[REDACTED]")
    return out

# Pollers manifest schema version history:
#
#   v1 (2026-05-26, chainlink #91): introduced the ``schema_version`` field.
#     Manifests without ``schema_version`` are treated as v1 for backwards
#     compatibility. Manifests with an unknown version emit a warning but are
#     still parsed on a best-effort basis (breaking changes require a bump).
#
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
    Returns ``False`` on subsequent failures (circuit re-armed, not newly
    tripped) or when the threshold hasn't been reached yet.
    """
    cb = _circuit_breakers.setdefault(name, _CircuitBreakerState())
    cb.consecutive_failures += 1
    # ``>=`` not ``==`` (chainlink #409): the counter only resets on a
    # clean run, so once it passes the threshold it keeps climbing —
    # with exact equality the single backoff window armed at the
    # threshold expires and a hard-down poller storms every tick
    # forever after. Every failure at or past the threshold re-arms
    # the window; only the threshold-crossing failure reports a trip.
    if cb.consecutive_failures >= POLLER_CIRCUIT_BREAKER_THRESHOLD:
        cb.disabled_until = time.time() + POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS
        return cb.consecutive_failures == POLLER_CIRCUIT_BREAKER_THRESHOLD
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
    #: ``recover_failed_turns`` (chainlink #262): opt into framework-side
    #: recovery of poller turns whose triggered turn FAILED. When True,
    #: ``run_poller`` stashes each enqueued event by ``source_id`` and, on
    #: the next cycle, reads ``turn_failed`` / ``turn_completed`` outcomes
    #: to re-enqueue (capped) the failed ones — closing the "poll advanced
    #: the cursor but the triggered turn died" drop (#299) for pollers with
    #: no live state to reconcile against (gmail, github issue/comment
    #: turns). OFF by default; leave OFF for pollers that recover another
    #: way — github-poller uses #516's ``requested_reviewers``
    #: reconciliation, so framework re-enqueue on top would double-fire
    #: review turns. See :mod:`mimir.poller_recovery`.
    recover_failed_turns: bool = False
    #: ``priority`` (priority-banded suppression): how much resource
    #: pressure this poller rides through before the scheduler sheds
    #: it. ``low`` yields at the first sign of pressure (ELEVATED),
    #: ``normal`` (default) sheds when quota is tight, ``high`` keeps
    #: firing until the provider actively refuses (recorded 429).
    #: See ``HomeostaticArbiter.should_fire`` for the fire matrix.
    #: Suppressed fires skip the subprocess entirely, so the poller's
    #: cursor stays frozen and catches up on the next tick after
    #: recovery — no events are lost, only delayed.
    priority: str = "normal"
    pass_env: tuple[str, ...] = ()
    #: ``env_required`` (chainlink #108): env var names the poller
    #: **must** have in its subprocess env to function correctly.
    #: Checked at the start of each ``run_poller`` invocation — after
    #: the env dict is fully assembled (allowlist + pass_env + env
    #: overrides). Any name that's absent from the final env causes the
    #: poller to skip that run, emit ``poller_missing_required_env``
    #: algedonically, and return 0 (no events enqueued). The intent
    #: is "fail loudly at runtime rather than silently misfire" —
    #: operators can see the algedonic signal and provision the missing
    #: var. Names in ``env_required`` that ARE in ``pass_env`` will
    #: naturally be present if the operator set the var; names that
    #: AREN'T in ``pass_env`` but are in the global allowlist also flow
    #: through. Secrets that aren't in either surface are always absent,
    #: so listing a deny-filter secret in ``env_required`` WITHOUT
    #: also listing it in ``pass_env`` is always a misconfiguration.
    env_required: tuple[str, ...] = ()
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
    #: ``deliver`` (chainlink #508): optional channel id the agent should
    #: deliver this poller's surfaced output to. Injected into the triggered
    #: turn's context as an instruction (the agent judges + uses send_message —
    #: NOT an auto-dump); on a hard turn failure the framework posts a ``⚠️``
    #: notice there. The literal ``OPERATOR_CHANNEL`` resolves to the operator
    #: alert channel. Unset → today's silent behavior. Does NOT change the
    #: event's own ``channel_id`` (the poller keeps its per-poller queue).
    deliver: str | None = None

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


#: Per-poller fields an operator may override from
#: ``<home>/pollers-overrides.yaml`` — the same set skill updates treat
#: as deployment tuning. The overrides file lives in the HOME, not the
#: skill directory: skill updates can never touch it, drift detection
#: never sees it, and a rotation/retune survives any reinstall. Mirrors
#: the ``scheduler.yaml`` pattern for callable crons (operator config
#: overrides shipped defaults at load time).
POLLER_OVERRIDE_KEYS = frozenset(
    {"cron", "priority", "batch_size", "recover_failed_turns", "env", "pass_env",
     "deliver"}
)

#: Recognized string spellings for boolean overrides. YAML 1.1 already maps
#: bare ``yes``/``no``/``on``/``off``/``true``/``false`` to real bools, but a
#: *quoted* value (``recover_failed_turns: "false"``) arrives as a string —
#: and ``bool("false")`` is ``True``. So coerce strictly rather than by Python
#: truthiness, or an operator turning a flag OFF could silently turn it ON.
_OVERRIDE_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_OVERRIDE_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


def _parse_override_bool(value: object) -> bool | None:
    """Strict bool coercion for an override value, or ``None`` if unparseable.

    ``None`` means "not a recognizable bool" — the caller keeps the manifest
    value and warns, never falling back to Python truthiness (which would make
    the quoted-``"false"`` YAML footgun read as ``True``).
    """
    if isinstance(value, bool):  # must precede the int check — bool ⊂ int
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _OVERRIDE_BOOL_TRUE:
            return True
        if s in _OVERRIDE_BOOL_FALSE:
            return False
    return None


def load_poller_overrides(path: Path | None) -> dict[str, dict]:
    """Parse ``pollers-overrides.yaml`` → ``{poller_name: {field: value}}``.

    Best-effort and fail-safe: a missing file is a no-op, a malformed
    file logs one warning and applies nothing (the manifests keep
    working), and unknown FIELDS inside an entry are dropped with a
    warning so a typo can't smuggle arbitrary keys into PollerConfig.
    Sync context (discovery runs before the event loop) → ``log.warning``,
    same as the other discovery diagnostics.
    """
    if path is None or not path.is_file():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — config parse must never abort discovery
        log.warning("poller_overrides_invalid: %s — %s; ignoring file", path, exc)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.warning(
            "poller_overrides_invalid: %s — root must be a mapping of "
            "poller name → overrides; ignoring file", path,
        )
        return {}
    out: dict[str, dict] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            log.warning(
                "poller_overrides_invalid_entry: %s — %r must map to a "
                "dict of fields; skipping", path, name,
            )
            continue
        kept = {}
        for key, value in entry.items():
            if key not in POLLER_OVERRIDE_KEYS:
                log.warning(
                    "poller_overrides_unknown_field: %s — %s.%s is not "
                    "overridable (allowed: %s); dropping",
                    path, name, key, ", ".join(sorted(POLLER_OVERRIDE_KEYS)),
                )
                continue
            kept[str(key)] = value
        if kept:
            out[str(name)] = kept
    return out


def _apply_poller_overrides(
    poller: "PollerConfig", overrides: dict, *, source: Path,
) -> "PollerConfig":
    """Return ``poller`` with validated operator overrides applied.

    Field-level fail-safety: each invalid value warns and keeps the
    manifest value rather than dropping the poller — an override typo
    must degrade to shipped behavior, never to a dead poller. Cron is
    validated here (lazy APScheduler import) because the scheduler's
    invalid-cron preservation path (#419) protects RELOADS, not first
    installs — an unvalidated bad override cron on a fresh start would
    skip the poller entirely.
    """
    import dataclasses

    updates: dict = {}
    if "cron" in overrides:
        cron = str(overrides["cron"]).strip()
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(cron)
            updates["cron"] = cron
        except Exception as exc:  # noqa: BLE001 — keep manifest cron on any parse failure
            log.warning(
                "poller_overrides_invalid_cron: %s — %s.cron=%r (%s); "
                "keeping manifest cron %r",
                source, poller.name, cron, exc, poller.cron,
            )
    if "priority" in overrides:
        raw_p = overrides["priority"]
        norm = normalize_priority(raw_p, default=poller.priority)
        if not (isinstance(raw_p, str) and raw_p.strip().lower() == norm):
            log.warning(
                "poller_overrides_invalid_priority: %s — %s.priority=%r "
                "(expected low|normal|high); keeping %r",
                source, poller.name, raw_p, norm,
            )
        updates["priority"] = norm
    if "batch_size" in overrides:
        try:
            bs = int(overrides["batch_size"])
            if bs >= 1:
                updates["batch_size"] = bs
            else:
                raise ValueError(bs)
        except (TypeError, ValueError):
            log.warning(
                "poller_overrides_invalid_batch_size: %s — %s.batch_size=%r; "
                "keeping %d", source, poller.name,
                overrides["batch_size"], poller.batch_size,
            )
    if "recover_failed_turns" in overrides:
        parsed = _parse_override_bool(overrides["recover_failed_turns"])
        if parsed is None:
            log.warning(
                "poller_overrides_invalid_recover_failed_turns: %s — "
                "%s.recover_failed_turns=%r (expected a bool: "
                "true/false/yes/no/1/0); keeping %r",
                source, poller.name, overrides["recover_failed_turns"],
                poller.recover_failed_turns,
            )
        else:
            updates["recover_failed_turns"] = parsed
    if "env" in overrides:
        env_o = overrides["env"]
        if isinstance(env_o, dict):
            updates["env"] = {str(k): str(v) for k, v in env_o.items()}
        else:
            log.warning(
                "poller_overrides_invalid_env: %s — %s.env must be a "
                "mapping; keeping manifest env", source, poller.name,
            )
    if "pass_env" in overrides:
        pe = overrides["pass_env"]
        if isinstance(pe, list) and all(isinstance(x, str) for x in pe):
            updates["pass_env"] = tuple(x.strip() for x in pe if x.strip())
        else:
            log.warning(
                "poller_overrides_invalid_pass_env: %s — %s.pass_env must "
                "be a list of strings; keeping manifest pass_env",
                source, poller.name,
            )
    if "deliver" in overrides:  # chainlink #508
        dv = overrides["deliver"]
        if dv is None or isinstance(dv, str):
            updates["deliver"] = (dv.strip() or None) if isinstance(dv, str) else None
        else:
            log.warning(
                "poller_overrides_invalid_deliver: %s — %s.deliver must be a "
                "string (channel id or OPERATOR_CHANNEL) or null; keeping %r",
                source, poller.name, poller.deliver,
            )
    if not updates:
        return poller
    log.info(
        "poller_overrides_applied: %s — %s: %s",
        source, poller.name, ", ".join(sorted(updates)),
    )
    return dataclasses.replace(poller, **updates)


def discover_pollers(
    skills_dir: Path,
    *,
    state_root: Path | None = None,
    invalid_manifests: list[tuple[Path, str]] | None = None,
    overrides_path: Path | None = None,
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

    # chainlink #420: poller names are the registry/job/persist_dir/
    # circuit-breaker key, so two manifests declaring the same name
    # would silently conflate all of that state (and overcount
    # installs). First successfully-parsed occurrence in the
    # deterministic sorted-rglob order wins; later duplicates are
    # skipped loudly, naming both manifests so the operator can see
    # which skill lost.
    seen_names: dict[str, Path] = {}

    for pollers_file in sorted(skills_dir.rglob("pollers.json")):
        # Skip manifests under hidden directories (observed live
        # 2026-06-11): ``skill_install`` keeps full pre-update snapshots
        # at ``<skill>/.pre-update-backup/<ts>/`` — INCLUDING the
        # snapshot's pollers.json — and ``rglob`` walks into them. With
        # the #420 first-wins duplicate-name guard, the backup manifest
        # (``.pre-…`` sorts before the skill's own ``pollers.json``)
        # SHADOWED the freshly-updated live manifest: the scheduler
        # registered the BACKUP directory as skill_dir and ran the old
        # poller.py every tick, while the duplicate warning sat only in
        # container stderr. (Pre-#420 last-wins silently picked the live
        # one, masking the hazard.) Hidden directories are never valid
        # skill roots — backups, scratch, VCS internals — so exclude
        # them from discovery wholesale.
        try:
            _rel_dirs = pollers_file.relative_to(skills_dir).parts[:-1]
        except ValueError:  # pragma: no cover — rglob yields children only
            _rel_dirs = ()
        if any(part.startswith(".") for part in _rel_dirs):
            log.info(
                "poller_manifest_hidden_dir_skipped: %s", pollers_file,
            )
            continue
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
            deliver = str(entry.get("deliver", "")).strip() or None  # chainlink #508
            if not name or not command or not cron:
                log.warning(
                    "poller_missing_fields: %s — entry %r",
                    pollers_file, entry,
                )
                continue
            # chainlink #420: duplicate-name guard. ``log.warning``
            # (sync context, same as the other discovery warnings —
            # ``log_event`` is async and there may be no loop here).
            if name in seen_names:
                log.warning(
                    "poller_duplicate_name: %s declares name=%r already "
                    "taken by %s; skipping duplicate (first occurrence "
                    "wins)",
                    pollers_file, name, seen_names[name],
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
            # chainlink #108: env_required — names the poller needs in its env.
            env_required_raw = entry.get("env_required", [])
            if not isinstance(env_required_raw, list):
                log.warning(
                    "poller_invalid_env_required: %s name=%r value=%r "
                    "(expected list of strings); ignoring",
                    pollers_file, name, env_required_raw,
                )
                env_required_raw = []
            env_required_clean: list[str] = []
            for item in env_required_raw:
                if not isinstance(item, str):
                    log.warning(
                        "poller_invalid_env_required_item: %s name=%r "
                        "item=%r (expected string); skipping",
                        pollers_file, name, item,
                    )
                    continue
                key = item.strip()
                if key:
                    env_required_clean.append(key)
            # chainlink #351/#357: don't even schedule a poller whose
            # required env is unset in the env that ``run_poller`` will actually
            # assemble. This must mirror runtime: scrubbed allowlist-filtered
            # os.environ + explicit pass_env + manifest env + injected STATE_DIR
            # / POLLER_NAME / MIMIR_HOME. Checking raw os.environ here schedules pollers that
            # later no-op every tick because the required key is denied; failing
            # to include injected keys skips pollers that would run.
            if env_required_clean:
                _env_avail = _poller_env_available_at_discovery(
                    env_raw=env_raw,
                    pass_env=pass_env_clean,
                )
                _missing_req = [k for k in env_required_clean if k not in _env_avail]
                if _missing_req:
                    log.warning(
                        "poller_skipped_unset_env: %s name=%r — not scheduling; "
                        "required env unset: %s",
                        pollers_file, name, ", ".join(_missing_req),
                    )
                    continue
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
            # chainlink #262: opt-in framework recovery of failed poller
            # turns. ``bool(...)`` coerces truthy json values; a stray
            # non-bool just reads as on/off rather than erroring (low-stakes
            # flag, unlike batch_size which affects coalescing math).
            recover_failed_turns = bool(entry.get("recover_failed_turns", False))
            # ``priority`` (priority-banded suppression): low | normal |
            # high. Garbage values fall back to the default with a
            # warning — a typo shouldn't silently promote a poller to
            # ride through quota pressure (or demote it to shed early).
            raw_priority = entry.get("priority")
            priority = "normal"
            if raw_priority is not None:
                priority = normalize_priority(raw_priority)
                if not (
                    isinstance(raw_priority, str)
                    and raw_priority.strip().lower() == priority
                ):
                    log.warning(
                        "poller_invalid_priority: %s name=%r value=%r "
                        "(expected low|normal|high); using %r",
                        pollers_file, name, raw_priority, priority,
                    )
            seen_names[name] = pollers_file
            pollers.append(
                PollerConfig(
                    name=name,
                    command=command,
                    cron=cron,
                    env={str(k): str(v) for k, v in env_raw.items()},
                    skill_dir=skill_dir,
                    persist_dir=persist_dir,
                    batch_size=batch_size,
                    recover_failed_turns=recover_failed_turns,
                    priority=priority,
                    pass_env=tuple(pass_env_clean),
                    env_required=tuple(env_required_clean),
                    manifest_path=pollers_file,
                    deliver=deliver,
                ),
            )

    # Operator overrides (``<home>/pollers-overrides.yaml``): applied
    # AFTER manifest parse + duplicate-name dedupe so the override keys
    # win over whatever the skill shipped. Unknown poller names warn —
    # a renamed/uninstalled poller shouldn't silently orphan its tuning.
    overrides = load_poller_overrides(overrides_path)
    if overrides:
        by_name = {p.name for p in pollers}
        for name in sorted(set(overrides) - by_name):
            log.warning(
                "poller_overrides_unknown_poller: %s — %r has no installed "
                "poller; overrides not applied", overrides_path, name,
            )
        pollers = [
            _apply_poller_overrides(p, overrides[p.name], source=overrides_path)
            if p.name in overrides else p
            for p in pollers
        ]
    return pollers


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a shell poller and any child process it spawned.

    ``create_subprocess_shell`` starts a shell wrapper; killing only the
    shell can leave the actual poller child holding stdout/stderr pipes
    open, which makes timeout handling hang while drain tasks wait for
    EOF. Pollers are launched in their own session, so POSIX platforms can
    kill the whole process group. Fall back to ``proc.kill()`` where
    process groups are unavailable.
    """
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback
            proc.kill()
    except ProcessLookupError:
        pass


async def _drain_capped(
    stream: "asyncio.StreamReader | None",
    limit: int,
    on_overflow: Callable[[], None],
) -> bytes:
    """Read from *stream* up to *limit* bytes, then stop accumulating.

    chainlink #258: bounds memory regardless of how much the subprocess
    writes. Once cumulative output exceeds *limit*, invoke *on_overflow*
    (which kills the process so both pipes EOF) and keep draining to EOF
    but discard the excess — so the killed process's pipe closes cleanly
    without buffering gigabytes. Returns the first *limit* bytes.
    """
    if stream is None:
        return b""
    buf = bytearray()
    overflowed = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if not overflowed:
            buf.extend(chunk)
            if len(buf) > limit:
                del buf[limit:]
                overflowed = True
                on_overflow()
        # Past the cap: keep reading to EOF (the kill closes the pipe
        # shortly) but discard — never grow `buf` beyond `limit`.
    return bytes(buf)


async def run_poller(
    poller: PollerConfig,
    *,
    enqueue: Callable[[AgentEvent], Awaitable[bool]],
    timeout: float = POLLER_TIMEOUT_SECONDS,
    home: Path | None = None,
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

    ``home`` is the authoritative agent home path supplied by the scheduler
    from ``Config.home``. It is injected as ``MIMIR_HOME`` for pollers that
    need to resolve files under the agent home without depending on host env.

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

    # chainlink #262: framework recovery of prior failed turns. Before this
    # cycle's poll, reconcile in-flight events against turn outcomes —
    # re-enqueue (capped) the ones whose triggered turn died, drop the ones
    # that completed. Opt-in per poller (off by default). Runs only when the
    # circuit is closed (the poll is proceeding); a poll-side outage defers
    # recovery until the poller is healthy again — the recovery watermark
    # spans the gap, so nothing is lost. Wrapped so a recovery hiccup can
    # never break the poll cycle that follows.
    if poller.recover_failed_turns:
        _events_path = get_events_path()
        if _events_path is not None:
            try:
                _rec = await poller_recovery.reconcile_failed_turns(
                    poller_name=poller.name,
                    channel_id=poller.channel_id(),
                    persist_dir=persist_dir,
                    events_path=_events_path,
                    enqueue=enqueue,
                )
                if _rec["reenqueued"] or _rec["gave_up"]:
                    await log_event(
                        "poller_recovery",
                        poller=poller.name,
                        reenqueued=_rec["reenqueued"],
                        completed=_rec["completed"],
                        gave_up=_rec["gave_up"],
                    )
            except Exception as exc:  # noqa: BLE001 — recovery must not break polling
                log.warning(
                    "poller recovery: reconcile failed for %s: %s",
                    poller.name, exc,
                )

    # CR2 (external I/O) fix: previously this passed ``{**os.environ,
    # **poller.env}`` — the entire mimir process env (including
    # MIMIR_API_KEY, ANTHROPIC_API_KEY, DISCORD_TOKEN,
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
    # custom-CA containers, noexec-/tmp setups). Skill authors who need
    # additional keys use manifest ``env``, manifest ``pass_env``, or the
    # global ``MIMIR_POLLER_ENV_ALLOWLIST``. Keep this path in sync with
    # ``_poller_env_available_at_discovery``.
    env = {k: v for k, v in os.environ.items() if _allowed_poller_env_key(k)}
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
        # chainlink #229: hard-deny process-control / loader vars BEFORE
        # the os.environ check, so the deny fires even when the var is
        # set in os.environ (the only interesting case from a security
        # standpoint — if it's unset, propagation is moot).
        if key in _PROCESS_CONTROL_ENV_DENY:
            await log_event(
                "poller_env_process_control_blocked",
                poller=poller.name,
                key=key,
            )
            continue
        if key not in os.environ:
            continue
        env[key] = os.environ[key]
        if any(key.endswith(s) for s in _DENY_ENV_SUFFIXES) or any(
            key.startswith(p) for p in _DENY_ENV_PREFIXES
        ):
            await log_event(
                "poller_env_passthrough_named_secret",
                poller=poller.name,
                key=key,
            )
    # Explicit per-skill ``env`` overlay still wins — EXCEPT for the
    # chainlink #229 process-control hard-denies (chainlink #421). The
    # manifest map is literal operator config, but it's also the one
    # env surface a malicious/compromised skill fully controls; an
    # ``env: {"LD_PRELOAD": ...}`` entry would hijack the loader of
    # every subsequent run. The #95 check below only matches
    # secret-NAME patterns (and only warns), so the hard-deny has to
    # fire here, at assembly time — same block-and-surface contract as
    # the pass_env loop above: key logged, value never.
    for key, value in poller.env.items():
        if key in _PROCESS_CONTROL_ENV_DENY:
            await log_event(
                "poller_env_process_control_blocked",
                poller=poller.name,
                key=key,
            )
            continue
        env[key] = value
    # chainlink #95: warn when poller.env re-introduces a key whose name
    # matches the deny-list patterns.  ``pass_env`` is the documented path
    # for forwarding live secrets from os.environ; ``poller.env`` is the
    # literal-value path meant for static config — not secrets.  Operators
    # who accidentally write ``env: { MY_API_KEY: "${MY_API_KEY}" }`` in
    # pollers.json re-expose a denied key without realising it (the
    # ``${…}`` is NOT shell-expanded — pollers.py coerces values to str
    # at parse time, so the literal string "${MY_API_KEY}" reaches the
    # subprocess, which is confusing but also signals the operator put
    # the wrong thing in ``env``).  Emit a negative event so the algedonic
    # block surfaces this configuration smell.  The value is NOT logged.
    for key in poller.env:
        if any(key.endswith(s) for s in _DENY_ENV_SUFFIXES) or any(
            key.startswith(p) for p in _DENY_ENV_PREFIXES
        ):
            await log_event(
                "poller_env_secret_reintroduced",
                poller=poller.name,
                key=key,
            )
    env["STATE_DIR"] = str(persist_dir)
    env["POLLER_NAME"] = poller.name
    # Scheduler passes Config.home here. Direct test/niche callers that omit
    # it still get a deterministic home path from the install layout
    # (``<home>/skills/<skill>`` → home) rather than reading
    # os.environ["MIMIR_HOME"], which may be absent or stale when mimir is
    # launched with --home.
    env["MIMIR_HOME"] = str(
        home if home is not None else poller.skill_dir.parent.parent
    )

    # chainlink #108: env_required validation — check after the env dict is
    # fully assembled (allowlist + pass_env + poller.env + injected vars).
    # Missing vars mean "don't run this tick and surface an algedonic signal."
    # The operator sees the event, provisions the var, and the next tick runs
    # cleanly.  We collect ALL missing names (not short-circuit) so the
    # operator can fix them all in one pass.
    if poller.env_required:
        missing = [k for k in poller.env_required if k not in env]
        if missing:
            await log_event(
                "poller_missing_required_env",
                poller=poller.name,
                missing=missing,
            )
            return 0

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
                start_new_session=True,
            )
            # chainlink #258: drain stdout/stderr with hard byte ceilings
            # rather than communicate() (which buffers the WHOLE stream
            # before any cap), so a runaway poller can't OOM mimir. On
            # overflow we kill the process — both pipes EOF, the drains
            # finish — and treat the tick as a failure instead of acting on
            # truncated output.
            _overflow = {"hit": False}

            def _on_overflow() -> None:
                if not _overflow["hit"]:
                    _overflow["hit"] = True
                    _kill_process_group(proc)

            stdout_task = asyncio.create_task(
                _drain_capped(
                    proc.stdout, MAX_POLLER_STDOUT_BYTES, _on_overflow,
                ),
            )
            stderr_task = asyncio.create_task(
                _drain_capped(
                    proc.stderr, MAX_POLLER_STDERR_BYTES, _on_overflow,
                ),
            )
            _, pending = await asyncio.wait(
                {stdout_task, stderr_task}, timeout=timeout,
            )
            if pending:
                _kill_process_group(proc)
                await proc.wait()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise asyncio.TimeoutError
            stdout_bytes = stdout_task.result()
            stderr_bytes = stderr_task.result()
            # chainlink #410: the ``asyncio.wait`` above bounds only the
            # pipe drains. Both drains hitting EOF means the child closed
            # its fds, not that it exited — a poller that closes stdout/
            # stderr and keeps running would hang a bare ``wait()`` here
            # with the caller's semaphore slot pinned. Bound the reap and
            # route an overrun into the existing timeout path
            # (``poller_timeout`` event + circuit-breaker failure).
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=min(POLLER_EXIT_GRACE_SECONDS, timeout),
                )
            except asyncio.TimeoutError:
                _kill_process_group(proc)
                await proc.wait()
                raise
            if _overflow["hit"]:
                await log_event(
                    "poller_output_overflow",
                    poller=poller.name,
                    stdout_limit_bytes=MAX_POLLER_STDOUT_BYTES,
                    stderr_limit_bytes=MAX_POLLER_STDERR_BYTES,
                )
                if _cb_record_failure(poller.name):
                    await log_event(
                        "poller_circuit_tripped",
                        poller=poller.name,
                        consecutive_failures=_circuit_breakers[
                            poller.name
                        ].consecutive_failures,
                        backoff_seconds=POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS,
                        reason="output_overflow",
                    )
                return 0
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
            _kill_process_group(proc)
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
            stderr=_redact_poller_env_values(
                stderr_text,
                env,
            )[:POLLER_STDERR_LOG_CHARS],
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
                line=_redact_poller_env_values(
                    line,
                    env,
                )[:POLLER_INVALID_LINE_CHARS],
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
        if poller.deliver:  # chainlink #508 — raw value; resolved at turn time
            extra["deliver"] = poller.deliver
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
            # chainlink #262: stash the enqueued event so a later failed
            # turn can re-enqueue it (opt-in per poller; off by default).
            if poller.recover_failed_turns:
                poller_recovery.stash_enqueued_event(persist_dir, event)
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
    "POLLER_EXIT_GRACE_SECONDS",
    "POLLER_PROMPT_CHARS",
    "POLLER_CIRCUIT_BREAKER_THRESHOLD",
    "POLLER_CIRCUIT_BREAKER_BACKOFF_SECONDS",
    "_circuit_breakers",  # exposed for tests + introspection; prefixed as internal
)
