"""Credential verification probes (SPEC §16 item 14 — credential
rotation, Phase 2).

Phase 1 (``docs/credentials.md``) cataloged every credential mimir
consumes and named the verification probe for each. This module
encodes those probes as a callable registry so:

- The operator can run a probe before/after rotation:
  ``mimir verify-cred GITHUB_TOKEN`` → exit 0 if live, 1 if stale.
- The rotation tool (Phase 3) can call ``verify(name)`` inline to
  decide whether to commit the new value or roll back.
- Each probe is independently testable — no need to stand up the
  full agent to verify a single credential.

Probe contract:

- Each probe is an idempotent, side-effect-free check that the
  credential reaches the upstream service. Probes do NOT consume
  any quota / tokens beyond what's needed to confirm authentication
  (typically an ``auth status`` / ``whoami`` style call).
- Probes return a :class:`ProbeResult` carrying ``ok`` (bool) +
  a one-line ``detail`` for operator-facing reporting + the cred
  ``type`` (A/B/C/D from docs/credentials.md).
- A probe MAY be ``UNAVAILABLE`` (the tool isn't installed, the
  envvar isn't set, etc.). That's not the same as "stale credential"
  — it's "can't probe". Surface as ``ok=False`` with a distinguishing
  detail prefix; the rotation tool can decide whether to treat
  unavailable as fatal.

Type B (long-lived bridge) and Type C (OAuth refresh) probes are
stubbed — they need events.jsonl scanning / process-state inspection
that's better staged into Phase 3. The registry surfaces them as
``not_implemented`` so the CLI lists them rather than silently
omitting them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Literal


CredType = Literal["A", "B", "C", "D"]


@dataclass
class ProbeResult:
    """Outcome of a single credential probe."""

    name: str
    cred_type: CredType
    ok: bool
    detail: str

    def render(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return f"[{self.cred_type}] {status}  {self.name}: {self.detail}"


@dataclass
class Probe:
    """Registry entry. ``fn`` runs the probe and returns
    ``(ok, detail)``; the registry wraps it in :class:`ProbeResult`
    so callers get the name + type for free."""

    name: str
    cred_type: CredType
    env_vars: tuple[str, ...]  # which env vars feed this credential
    description: str           # one-liner for ``mimir verify-creds`` listing
    fn: Callable[[], tuple[bool, str]]


# ── helpers ──────────────────────────────────────────────────────────


def _env_set(name: str) -> tuple[bool, str | None]:
    """``(True, value)`` if env var is set + non-empty, else
    ``(False, None)``."""
    v = os.environ.get(name, "").strip()
    return (bool(v), v if v else None)


def _all_env_set(*names: str) -> tuple[bool, str]:
    """``(True, "")`` if every name is set + non-empty, else
    ``(False, "missing: <comma-list>")``."""
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        return (False, f"missing env: {', '.join(missing)}")
    return (True, "")


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _run_quiet(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a probe subprocess. Returns ``(rc, stdout, stderr)``. Times
    out at ``timeout`` seconds; on timeout returns ``rc=124`` (the
    coreutils ``timeout`` convention) so callers can disambiguate."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _unavailable(reason: str) -> tuple[bool, str]:
    return (False, f"unavailable: {reason}")


def _not_implemented(typ: str) -> tuple[bool, str]:
    return (False, f"not_implemented: Type {typ} probe pending Phase 3")


# ── Type A probes ────────────────────────────────────────────────────


def _probe_github_token() -> tuple[bool, str]:
    if not _has_binary("gh"):
        return _unavailable("`gh` CLI not installed")
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        return _unavailable("GITHUB_TOKEN not set")
    rc, out, err = _run_quiet(["gh", "auth", "status"])
    if rc == 0:
        # gh writes the authenticated login to stderr (annoyingly).
        # Surface the first non-empty line so the operator sees who
        # the token belongs to.
        first = next((l for l in (err.splitlines() + out.splitlines()) if l.strip()), "")
        return (True, first or "gh auth status ok")
    return (False, (err.splitlines() or ["gh auth status failed"])[0])


def _probe_acli_token() -> tuple[bool, str]:
    if not _has_binary("acli"):
        return _unavailable("`acli` not installed")
    ok, missing = _all_env_set("ACLI_TOKEN", "ACLI_EMAIL", "ACLI_SITE")
    if not ok:
        return _unavailable(missing)
    rc, out, err = _run_quiet(["acli", "jira", "auth", "status"])
    if rc == 0:
        return (True, "acli authenticated")
    return (False, (out.splitlines() + err.splitlines() or ["acli auth status failed"])[0])


def _probe_op_token() -> tuple[bool, str]:
    if not _has_binary("op"):
        return _unavailable("`op` (1Password CLI) not installed")
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip():
        return _unavailable("OP_SERVICE_ACCOUNT_TOKEN not set")
    rc, out, err = _run_quiet(["op", "whoami"])
    if rc == 0:
        return (True, out or "op authenticated")
    return (False, (err.splitlines() or ["op whoami failed"])[0])


def _probe_openweather_key() -> tuple[bool, str]:
    """OpenWeather has no auth-status endpoint. Format-check only:
    confirm the env var is set + has the right shape (32-char hex).
    The first real ``weather`` invocation will surface a 401 if the
    key is stale."""
    ok, value = _env_set("OPENWEATHER_API_KEY")
    if not ok:
        return _unavailable("OPENWEATHER_API_KEY not set")
    assert value is not None  # narrowing for mypy
    if len(value) != 32 or not all(c in "0123456789abcdef" for c in value.lower()):
        return (False, f"format wrong (expected 32 hex chars, got {len(value)})")
    return (True, "format ok (live call deferred to first ``weather`` use)")


# ── Type D probes ────────────────────────────────────────────────────


def _probe_static_key_format(env: str, *, prefix: str | None = None,
                             min_len: int = 16) -> tuple[bool, str]:
    """Generic format check for a static API key. Confirms the env
    var is set, non-trivially long, and (if ``prefix`` given) starts
    with the expected provider prefix. Live API calls are deferred
    to the first real use — running a probe burns tokens for no
    additional safety beyond a format check."""
    ok, value = _env_set(env)
    if not ok:
        return _unavailable(f"{env} not set")
    assert value is not None
    if len(value) < min_len:
        return (False, f"format wrong (too short, got {len(value)} chars)")
    if prefix is not None and not value.startswith(prefix):
        return (False, f"format wrong (expected prefix {prefix!r})")
    return (True, f"format ok ({len(value)} chars{' ' + prefix if prefix else ''})")


def _probe_anthropic_api_key() -> tuple[bool, str]:
    return _probe_static_key_format("ANTHROPIC_API_KEY", prefix="sk-ant-")


def _probe_voyage_api_key() -> tuple[bool, str]:
    return _probe_static_key_format("VOYAGE_API_KEY", prefix="pa-")


def _probe_openai_api_key() -> tuple[bool, str]:
    return _probe_static_key_format("OPENAI_API_KEY", prefix="sk-")


def _probe_tavily_api_key() -> tuple[bool, str]:
    return _probe_static_key_format("TAVILY_API_KEY", prefix="tvly-")


def _probe_mimir_api_key() -> tuple[bool, str]:
    # Operator-chosen — no prefix convention. Just length.
    return _probe_static_key_format("MIMIR_API_KEY", min_len=16)


def _probe_x_oauth_quartet() -> tuple[bool, str]:
    """X's OAuth 1.0a needs all four env vars present and signed
    consistently. The format check confirms presence + non-trivial
    length; ``social-cli whoami -p x`` is the real live probe (added
    in Type-B-style Phase 3)."""
    ok, missing = _all_env_set(
        "X_API_KEY", "X_API_SECRET",
        "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
    )
    if not ok:
        return _unavailable(missing)
    return (True, "format ok (live: ``social-cli whoami -p x``)")


def _probe_bsky_app_password() -> tuple[bool, str]:
    """Bluesky app passwords are 4-block hyphenated (xxxx-xxxx-xxxx-xxxx).
    Accepted env var name varies between deployments — accept either."""
    ok, value = _env_set("ATPROTO_APP_PASSWORD")
    if not ok:
        ok, value = _env_set("BSKY_APP_PASSWORD")
    if not ok:
        return _unavailable("ATPROTO_APP_PASSWORD / BSKY_APP_PASSWORD not set")
    assert value is not None
    blocks = value.split("-")
    if len(blocks) != 4 or any(len(b) != 4 for b in blocks):
        return (False, "format wrong (expected xxxx-xxxx-xxxx-xxxx)")
    return (True, "format ok (live: ``social-cli whoami -p bsky``)")


# ── Type B + C probes (stubbed for Phase 3) ─────────────────────────


def _probe_discord_token() -> tuple[bool, str]:
    return _not_implemented("B")


def _probe_slack_tokens() -> tuple[bool, str]:
    return _not_implemented("B")


def _probe_claude_oauth() -> tuple[bool, str]:
    return _not_implemented("C")


def _probe_gmail_oauth() -> tuple[bool, str]:
    return _not_implemented("C")


# ── Registry ─────────────────────────────────────────────────────────


PROBES: dict[str, Probe] = {
    # Type A — subprocess re-spawn
    "GITHUB_TOKEN": Probe(
        name="GITHUB_TOKEN", cred_type="A", env_vars=("GITHUB_TOKEN",),
        description="GitHub PAT (gh CLI + git push to state repo)",
        fn=_probe_github_token,
    ),
    "ACLI_TOKEN": Probe(
        name="ACLI_TOKEN", cred_type="A",
        env_vars=("ACLI_TOKEN", "ACLI_EMAIL", "ACLI_SITE"),
        description="Atlassian CLI (Jira) — all 3 vars required",
        fn=_probe_acli_token,
    ),
    "OP_SERVICE_ACCOUNT_TOKEN": Probe(
        name="OP_SERVICE_ACCOUNT_TOKEN", cred_type="A",
        env_vars=("OP_SERVICE_ACCOUNT_TOKEN",),
        description="1Password service account token",
        fn=_probe_op_token,
    ),
    "OPENWEATHER_API_KEY": Probe(
        name="OPENWEATHER_API_KEY", cred_type="A",
        env_vars=("OPENWEATHER_API_KEY",),
        description="OpenWeather API key (weather skill)",
        fn=_probe_openweather_key,
    ),
    # Type B — long-lived bridge clients (Phase 3 will wire live probes)
    "DISCORD_TOKEN": Probe(
        name="DISCORD_TOKEN", cred_type="B", env_vars=("DISCORD_TOKEN",),
        description="Discord bridge bot token",
        fn=_probe_discord_token,
    ),
    "SLACK_TOKENS": Probe(
        name="SLACK_TOKENS", cred_type="B",
        env_vars=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        description="Slack bridge bot + app tokens",
        fn=_probe_slack_tokens,
    ),
    # Type C — OAuth refresh dance (Phase 3 will wire login-flow probes)
    "CLAUDE_OAUTH": Probe(
        name="CLAUDE_OAUTH", cred_type="C", env_vars=("MIMIR_CLAUDE_OAUTH_CREDENTIALS",),
        description="Claude Max OAuth (.credentials.json refresh token)",
        fn=_probe_claude_oauth,
    ),
    "GMAIL_OAUTH": Probe(
        name="GMAIL_OAUTH", cred_type="C", env_vars=(),
        description="Gmail / Google Workspace OAuth (gog keyring)",
        fn=_probe_gmail_oauth,
    ),
    # Type D — static API keys
    "ANTHROPIC_API_KEY": Probe(
        name="ANTHROPIC_API_KEY", cred_type="D", env_vars=("ANTHROPIC_API_KEY",),
        description="Anthropic API key (format-check only)",
        fn=_probe_anthropic_api_key,
    ),
    "VOYAGE_API_KEY": Probe(
        name="VOYAGE_API_KEY", cred_type="D", env_vars=("VOYAGE_API_KEY",),
        description="Voyage AI embedding key (format-check only)",
        fn=_probe_voyage_api_key,
    ),
    "OPENAI_API_KEY": Probe(
        name="OPENAI_API_KEY", cred_type="D", env_vars=("OPENAI_API_KEY",),
        description="OpenAI API key (format-check only)",
        fn=_probe_openai_api_key,
    ),
    "TAVILY_API_KEY": Probe(
        name="TAVILY_API_KEY", cred_type="D", env_vars=("TAVILY_API_KEY",),
        description="Tavily web-search API key (format-check only)",
        fn=_probe_tavily_api_key,
    ),
    "MIMIR_API_KEY": Probe(
        name="MIMIR_API_KEY", cred_type="D", env_vars=("MIMIR_API_KEY",),
        description="mimir's own HTTP server API gate (format-check only)",
        fn=_probe_mimir_api_key,
    ),
    "X_OAUTH": Probe(
        name="X_OAUTH", cred_type="D",
        env_vars=("X_API_KEY", "X_API_SECRET",
                  "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"),
        description="X OAuth 1.0a — all 4 vars required",
        fn=_probe_x_oauth_quartet,
    ),
    "BSKY_APP_PASSWORD": Probe(
        name="BSKY_APP_PASSWORD", cred_type="D",
        env_vars=("ATPROTO_APP_PASSWORD", "BSKY_APP_PASSWORD"),
        description="Bluesky app password (format-check only)",
        fn=_probe_bsky_app_password,
    ),
}


def verify(name: str) -> ProbeResult:
    """Run a single probe by registry name. Raises ``KeyError`` if
    ``name`` isn't registered — callers that want a soft failure
    should use ``name in PROBES`` first."""
    probe = PROBES[name]
    ok, detail = probe.fn()
    return ProbeResult(
        name=probe.name, cred_type=probe.cred_type, ok=ok, detail=detail,
    )


def verify_all() -> list[ProbeResult]:
    """Run every probe in registry order. Used by ``mimir verify-creds``
    for a full-deployment health check."""
    return [verify(name) for name in PROBES]


# ── CLI entrypoints (wired in cli.py) ───────────────────────────────


def run_verify_cred_cmd(name: str) -> int:
    """``mimir verify-cred <name>`` entrypoint. Returns the exit code
    callers should pass to ``sys.exit``: 0 if live, 1 if stale /
    unavailable / not-implemented, 2 if ``name`` doesn't exist."""
    if name not in PROBES:
        avail = ", ".join(sorted(PROBES))
        print(f"unknown credential: {name!r}")
        print(f"  registered: {avail}")
        return 2
    result = verify(name)
    print(result.render())
    return 0 if result.ok else 1


def run_verify_creds_cmd(only_type: str | None = None) -> int:
    """``mimir verify-creds [--type X]`` entrypoint. Returns 0 if all
    listed probes are ``ok``, 1 if any fail. Filters by ``only_type``
    when set (one of A/B/C/D).
    """
    results = verify_all()
    if only_type:
        results = [r for r in results if r.cred_type == only_type]
    if not results:
        print(f"no probes registered for type {only_type!r}")
        return 1
    failures = 0
    for r in results:
        print(r.render())
        if not r.ok:
            failures += 1
    print()
    print(f"{len(results) - failures}/{len(results)} probes ok")
    return 0 if failures == 0 else 1
