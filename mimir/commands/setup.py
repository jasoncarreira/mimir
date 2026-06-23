"""Setup subcommand implementation for ``mimir setup``.

Extracted from ``mimir/cli.py`` — all setup-related constants, helpers,
and the public ``setup_home``, ``_print_setup_report``, and
``regenerate_api_key`` functions live here.  ``mimir.cli`` re-exports
them for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path
from textwrap import dedent

import yaml

from ..skill_defs import seed_skills
from ..subagent_defs import seed_subagent_defs
from ..memory_templates import (
    DEFAULT_ACTION_BOUNDARIES,
    DEFAULT_FILING_RULES,
    DEFAULT_HEARTBEAT_PATTERNS,
    DEFAULT_IDENTITY_MD,
    DEFAULT_LEARNED_BEHAVIORS,
    DEFAULT_NON_GOALS,
    DEFAULT_REFLECTION_POLICY,
    DEFAULT_VSM_TERMS,
    INIT_BLOCK_NAME,
    seed_core_memory,
    seed_init_block,
)

log = logging.getLogger(__name__)

DEFAULT_ENV_TEMPLATE = dedent(
    """\
    # mimir environment — fill in what you use, leave the rest blank.

    # ---- Agent chat model ------------------------------------------------
    # MIMIR_MODEL_SPEC has the form ``<provider>:<model>``. Examples:
    #
    #   anthropic:claude-sonnet-4-6       (default — direct Anthropic API)
    #   claude-code:claude-sonnet-4-6     (legacy Max OAuth subprocess; opt
    #                                      in via ``mimir setup --subscription``)
    #   anthropic:MiniMax-M2.7            (Minimax via Anthropic-compat —
    #                                      also set ANTHROPIC_BASE_URL)
    #   anthropic:kimi-k2-0905-preview    (Moonshot Kimi)
    #   openai:gpt-4.1-mini               (direct OpenAI)
    #
    # ``mimir setup --model <name>`` auto-detects the right prefix +
    # writes ANTHROPIC_BASE_URL when the provider needs one (Minimax,
    # Moonshot). Without --model, mimir uses ``anthropic:claude-sonnet-4-6``.
    MIMIR_MODEL_SPEC=

    # ---- LLM gateway (Anthropic-compatible) ------------------------------
    # For Claude direct: set ANTHROPIC_API_KEY.
    # For Minimax / Moonshot / other gateways: set ANTHROPIC_BASE_URL +
    # ANTHROPIC_API_KEY (the gateway's key under that name; mimir's
    # langchain-anthropic provider reads both env vars).
    ANTHROPIC_API_KEY=
    ANTHROPIC_BASE_URL=
    ANTHROPIC_AUTH_TOKEN=

    # ---- saga memory (in-process) ----------------------------------------
    # saga runs in-process — no endpoint or key to configure here.
    # OpenAI key for saga's embeddings (text-embedding-3-small) AND for
    # the bench harness's gpt-4o judge. Optional: if you leave it blank,
    # saga falls back to fastembed (local CPU, BAAI/bge-small-en-v1.5,
    # ~5ms/batch, no API cost). Bench parity vs the historical 0.774
    # baseline requires text-embedding-3-small, so set this if you're
    # running benchmarks. Daily mimir use is fine on the local fallback.
    OPENAI_API_KEY=

    # ---- Channel bridges (all optional) ----------------------------------
    DISCORD_TOKEN=
    SLACK_BOT_TOKEN=
    SLACK_APP_TOKEN=
    BSKY_HANDLE=
    BSKY_APP_PASSWORD=

    # ---- Server tuning ---------------------------------------------------
    MIMIR_WEB_PORT=8080
    MIMIR_EFFORT=high
    # API key for the public injection endpoint (POST /event). When set,
    # requests must carry a matching ``X-API-Key`` header. The server
    # binds to 0.0.0.0 — any non-localhost deployment must have this set.
    # ``mimir setup`` auto-generates a value here on first run; rotate
    # with ``mimir regenerate-api-key``. Leave blank only for local-dev
    # mode where you understand the implications.
    MIMIR_API_KEY=

    # ---- Usage / cost surfacing ------------------------------------------
    # Optional dollar ceilings that gate the "% of budget" annotation in
    # the turn prompt's Resource usage section. These are operator-set
    # cost thresholds; complementary to (not a replacement for) the
    # plan's unit budget — the SDK's RateLimitEvent stream feeds the
    # actual five_hour / seven_day / etc. utilization into the same
    # prompt section under "Plan windows."
    MIMIR_USAGE_5H_LIMIT_USD=
    MIMIR_USAGE_WEEKLY_LIMIT_USD=

    # Cost-rate alert. When current $/hr (last hour) exceeds either
    # threshold, a cost_rate_alert event lands in events.jsonl and the
    # algedonic + Resource usage sections annotate the alert. Both
    # optional. Spike ratio compares last-hour rate against the rolling
    # 7-day baseline (default 3.0 = "alert when current is >3× baseline").
    # Cooldown gates re-emission so the firehose doesn't churn.
    MIMIR_COST_HOURLY_LIMIT_USD=
    MIMIR_COST_RATE_SPIKE_RATIO=3.0
    # Floor on rate_now below which the spike check is silenced (an
    # asymmetry fix — a normal session burning a few cents/hour shouldn't
    # trip just because the rolling baseline is tiny). 0 disables.
    MIMIR_COST_RATE_SPIKE_FLOOR_USD=5.00
    MIMIR_COST_ALERT_COOLDOWN_MINUTES=60

    # Per-response plan-window capture. When true (default), the SDK
    # is configured with include_partial_messages so the streaming
    # message_start event carries a rate_limits block — Claude.ai
    # subscribers see current 5h / 7d / Opus / Sonnet / overage
    # utilization on every API response. Set false to skip the
    # streaming overhead at the cost of less-current plan data
    # (you'll only see numbers on transition events).
    MIMIR_CAPTURE_RATE_LIMITS=true

    # ---- Operator config -------------------------------------------------
    # Channel the agent uses for high-priority signals to you that don't fit
    # the current conversation (critical errors, urgent heartbeat findings,
    # dispatch failures). Leave blank to disable. Use a DM *conversation*
    # channel_id (NOT a user id) — typically your DM with the bot, e.g.
    # dm-slack-D05XXXX (the IM id) or dm-discord-NNN (the DM channel snowflake).
    # Tip: DM the bot once, then check the captured id via the agent's
    # list_channels tool or your dm_channels entry in state/identities.yaml.
    MIMIR_OPERATOR_ALERT_CHANNEL=
    """
)


# The bundled scheduler_template.yaml under mimir/ is the canonical
# default scheduler config; setup_home seeds it via seed_scheduler().
# A constant lived here originally but it diverged from the bundled
# file used by server.py startup — single source now in
# mimir/scheduler_template.yaml.


DEFAULT_HEARTBEAT_BACKLOG = dedent(
    """\
    # Heartbeat Backlog

    Tasks for autonomous work during scheduled heartbeats. Operator and
    agent both append.

    Format per item:

    ```
    - [ ] **<short name>** [YYYY-MM-DD added] — one-line what
      - What: ...
      - Why: ...
      - How: ...
      - Frequency: <daily | weekly | once>
      - Priority: <HIGH | MEDIUM | LOW>
      - Last completed: <YYYY-MM-DD or "never">
      - Skill: <relative path or skill name, optional>
    ```

    ## Active Backlog

    (Discrete tasks; pick one per heartbeat. Operator seeds initial
    items here; agent may append observations.)

    ## Standing Tasks

    (Daily / weekly / recurring; agent updates `Last completed:` after
    each run. Pick one whose slot for today is open.)
    """
)


# The bundled heartbeat.md / reflect.md under mimir/prompt_templates/
# are the canonical scheduled-tick prompts; setup_home seeds them via
# seed_prompts(). The short "trigger" constants that used to live here
# (DEFAULT_HEARTBEAT_PROMPT / DEFAULT_REFLECT_PROMPT) were a relic of
# when heartbeat/reflection were bundled SKILLS — the agent would
# receive a 5-line trigger and dispatch to the skill. Post-unbundling
# the scheduler loads the full workflow body directly.


DEFAULT_ISSUES_README = dedent(
    """\
    <!-- desc: what goes in memory/issues/ — operational-gotcha layer -->
    # memory/issues/

    Every-turn-discoverable operational gotchas. Each file is a
    fingerprint-shaped runbook for a failure mode mimir might hit
    again — the kind of note where the value is in the future-mimir
    matching a fresh symptom against a stored entry.

    Each entry's first-line `<!-- desc: ... -->` surfaces in the
    every-turn `memory/INDEX.md` description list, so the title +
    one-line desc need to make hash-lookup against a future symptom
    obvious. Body covers what triggered it, what the failure looked
    like, and the runbook fix.

    ## What goes elsewhere

    - **Concept-level synthesis** (pattern frameworks, theoretical
      models, named patterns from external sources) →
      `state/wiki/concepts/`. Concepts answer "how do I think about
      X?"; issues answer "what do I do when X happens again?"
    - **Long-form synthesis** (>5 KB writeups, baseline analyses,
      runner architectures) → `state/wiki/topics/`. Issues stay
      tight enough for fingerprint-matching at a glance.
    - **Channel-scoped facts** (operator preferences, channel-specific
      patterns) → `memory/channels/<id>/`. If a gotcha is specific to
      one channel, it's a channel fact, not a global issue.

    See `memory/core/60-filing-rules.md` for the full rubric and the
    misfiling table.
    """
)


#: Supported ``--embedding`` preset names for ``mimir setup``.
#: Each preset writes a tailored ``[embedding]`` block in the
#: generated saga.toml. Voyage is the default per the LongMemEval
#: cross-bench (Phase 3, 2026-05-12): voyage-4-lite beat OpenAI
#: text-embedding-3-small by 2.4pp aggregate / 4.5pp multi-session.
#:
#: Note: the operator-facing preset name "fastembed" maps to saga's
#: provider name "onnx" in the generated saga.toml — saga's provider
#: class is ``ONNXProvider`` (fastembed under the hood; saga's
#: registry key predates the fastembed library merger). The
#: ``mimir setup --embedding fastembed`` shortcut is meant to be
#: discoverable; operators reading saga.toml will see
#: ``provider = "onnx"`` and need to know they're the same thing.
EMBEDDING_PRESETS: tuple[str, ...] = ("voyage", "openai", "fastembed")
DEFAULT_EMBEDDING_PRESET = "voyage"


def _embedding_block_for_preset(preset: str) -> str:
    """Return the ``[embedding]`` block text for the named preset.

    All presets pair with ``[consolidation] similarity_threshold =
    "auto"`` (also written by ``_default_saga_toml``), which lets saga
    resolve the right threshold per provider at boot.
    """
    if preset == "voyage":
        return dedent(
            """\
            [embedding]
            # Voyage AI voyage-4-lite — LongMemEval cross-bench winner
            # (Phase 3, 2026-05-12): 0.904 aggregate vs OpenAI's 0.880
            # at $0.02/1M tokens + 200M signup free credit. saga's
            # ``provider = "voyage"`` shortcut sets the URL,
            # send_input_type, and api_key_env automatically — only the
            # model + dimensions are exposed here for operator visibility.
            provider = "voyage"
            model = "voyage-4-lite"
            dimensions = 1024
            api_key_env = "VOYAGE_API_KEY"
            """
        )
    if preset == "openai":
        return dedent(
            """\
            [embedding]
            # OpenAI text-embedding-3-small — the historical default;
            # bench-canonical at saga's default threshold 0.80. Use if
            # you don't have a Voyage account or prefer the OpenAI
            # ecosystem. Same per-token price ($0.02/1M) as voyage.
            provider = "openai"
            url = "https://api.openai.com/v1/embeddings"
            model = "text-embedding-3-small"
            dimensions = 1536
            api_key_env = "OPENAI_API_KEY"
            """
        )
    if preset == "fastembed":
        return dedent(
            """\
            [embedding]
            # fastembed BAAI/bge-small-en-v1.5 — fully local, no API
            # key required. Cold-start downloads the ~33MB ONNX model
            # into ~/.cache/fastembed/. Bench result on LongMemEval:
            # 0.72 aggregate / 0.5333 multi-session — 7pp below
            # hosted options. Right pick when offline / air-gapped /
            # zero-spend is the priority.
            provider = "onnx"
            model = "BAAI/bge-small-en-v1.5"
            dimensions = 384
            """
        )
    raise ValueError(
        f"unknown embedding preset: {preset!r}. "
        f"valid: {EMBEDDING_PRESETS}"
    )


def _default_saga_toml(
    home: Path,
    *,
    embedding: str = DEFAULT_EMBEDDING_PRESET,
) -> str:
    """v0.5 §2: saga.toml the in-process saga reads at boot.

    Defaults are saga's canonical post-fix settings (P30 + two-tier on,
    P12 query expansion on, supersedes_demotion on, confidence gating
    with low floor) plus mimir-specific overrides:

    - ``[storage].db_path`` lives under ``<home>/.mimir/`` next to mimir's
      own ``index.db``. Same directory, separate files: SQLite is
      single-writer per file, and saga's consolidation pass writes for
      several minutes (which would block mimir's per-turn reindexes if
      the file were shared).
    - ``[embedding]`` block is templated from one of ``EMBEDDING_PRESETS``
      (see ``_embedding_block_for_preset``); default is voyage per the
      Phase 3 LongMemEval cross-bench result.
    - ``[consolidation] similarity_threshold = "auto"`` resolves to the
      per-provider recommended value at boot — 0.92 for voyage / fastembed,
      0.80 for openai. See saga README for the sweep table.
    - ``[retrieval].enable_contextual_rewrite = true`` — mimir already
      passes ``context=`` on every query; flipping rewrite on means short
      referential queries ("yes, look for that") get resolved before
      retrieval. No-op when context is empty.
    - ``[triples].enable_extraction = true`` — populate the triples table
      on consolidation so future P41-style query-intent-gated work has
      data. ``[retrieval].enable_triple_augment_v2`` stays off (the
      post-fix bench showed -3.7pp multi-session, -2.3pp temporal).
    - No credential is written into this tracked file. saga runs in-process
      and needs no endpoint or key.
    """
    saga_dir = home / ".mimir"
    embedding_block = _embedding_block_for_preset(embedding)
    return dedent(
        f"""\
        # saga.toml — in-process saga config used by mimir.
        # Generated by `mimir setup`; safe to edit. mimir will not clobber
        # an existing file on re-run.

        [storage]
        db_path = "{saga_dir / 'saga.db'}"
        metrics_db_path = "{saga_dir / 'saga_metrics.db'}"
        # saga's default token_budget_ceiling is 40k — too low for any
        # real workload (single LongMemEval haystack alone exceeds it).
        # 1M is a comfortable production cap; integration benches that
        # ingest larger corpora bump this to 100M (matches saga_bench.toml).
        token_budget_ceiling = 1000000
        auto_compact_threshold_pct = 90
        refuse_threshold_pct = 99

        """
    ) + embedding_block + dedent(
        f"""\

        [llm]
        # Single LLM config for ALL saga internals: consolidation
        # synthesis, contextual rewrite, triple extraction, rerank,
        # subatom synthesis. saga.config.resolve_llm_config falls back
        # to this section when subsystems lack overrides; we don't set
        # any here so every call site gets the same model.
        #
        # provider = "claude_code" routes via claude-agent-sdk.query(),
        # which spawns a Claude Code subprocess and inherits Max OAuth
        # from ``claude login``. Free under Max (no API credit needed),
        # but each call has ~500ms-2s subprocess spawn overhead and
        # eats your 5h/7d windows. Right default for daily mimir use.
        #
        # For bench parity against saga_p30_canon_v4 (0.774, gpt-5.4-nano):
        #   provider = "openai_compat"
        #   url = "https://api.openai.com/v1/chat/completions"
        #   model = "gpt-5.4-nano"
        #   api_key_env = "OPENAI_API_KEY"
        #
        # For direct Anthropic API (paid credit, no Max OAuth):
        #   provider = "anthropic"
        #   model = "claude-haiku-4-5"
        #   api_key_env = "ANTHROPIC_API_KEY"
        provider = "claude_code"
        model = "claude-haiku-4-5"
        timeout_seconds = 60

        [retrieval]
        # v0.5 §2: rewrite short referential queries against the prior
        # conversation before retrieval. mimir always passes context=.
        enable_contextual_rewrite = true
        # Two-tier {{observations, raws}} is saga's canonical-best mode.
        two_tier_enabled = true
        # P30: retrieve atoms for the missing-reference pivot.
        enable_missing_ref_pivot = true
        # Confidence gating with low floor (drops sub-0.15 noise).
        enable_confidence_gating = true
        default_min_confidence_tier = "low"

        [retrieval_v2]
        # P12 (synonym expansion on the keyword pathway). The only
        # positive single lever since P30 — shipped to canonical.
        enable_query_expansion = true

        [triples]
        # Extract triples during consolidation so the graph table has data
        # for future query-intent-gated retrieval (P41). Augment is OFF
        # because P41-as-default regressed multi-session/temporal probes.
        enable_extraction = true

        [consolidation]
        enabled = true
        enable_llm = true
        # "auto" resolves to a per-provider value at boot — 0.92 for
        # voyage and fastembed (tight cosine distributions cap-saturate
        # at saga's historical default 0.80), 0.80 for openai. See saga
        # README for the full sweep table; swap to a literal float here
        # to override.
        similarity_threshold = "auto"

        # saga runs in-process by default and keeps no credential in this file.
        """
    )


DEFAULT_PROPOSED_CHANGES = dedent(
    """\
    # Proposed Changes (Legacy)

    Legacy pending HITL items from the pre-proposal-PR reflection workflow.
    Protected surfaces (`memory/core/*` and `prompts/*`) now use
    `open_proposal` / `submit_proposal` instead. Keep this file only for
    migration of existing entries or non-protected historical proposals.

    Format per item:

    ```
    ## YYYY-MM-DD — short title
    Source: <reflection week / heartbeat tick / other>
    Proposal: <what to change>
    Rationale: <why>
    Affected: <file paths or systems>
    Predicted effect: <measurable expectation, e.g. "error rate would drop"
                       or "Read tool would be invoked more often">
    ```

    The `Predicted effect:` line is what the legacy §12.2 audit pass
    measures against. Phrase it as something the agent could verify by
    reading events.jsonl / turns.jsonl: error-rate delta, tool-call
    frequency delta, etc. For new protected-surface changes, put this
    prediction in the proposal PR body instead.

    ## Pending

    (empty — populated by reflection)

    ## Applied

    (operator moves accepted items here, optionally with notes)

    ## Rejected

    (operator moves rejected items here, optionally with notes)
    """
)


DEFAULT_WIKI_AGENTS_MD = dedent(
    """\
    # AGENTS.md

    Schema for maintaining the wiki under ``state/wiki/``. The full skill
    is at ``skills/wiki/SKILL.md`` — this file is a quick reference.

    ## Three layers

    1. **Raw sources** — ``state/raw/`` — immutable source documents,
       never modified after landing.
    2. **Wiki** — ``state/wiki/`` — your synthesis with cross-references.
    3. **Schema** — this file — conventions for maintaining the wiki.

    ## Categories

    - ``entities/`` — named things (people, agents, organizations, products)
    - ``concepts/`` — abstract ideas, patterns, frameworks
    - ``topics/`` — concrete subjects, projects, events

    ## Conventions

    - Frontmatter: ``title``, ``description``, ``type``, optional ``tags``.
      Descriptive, not enforced — typos won't break anything.
    - Wikilinks: ``[[page-name]]``. Add inline in prose AND in a Related
      section. Links are not optional — they make the wiki a graph.
    - Each page should have a "Connection to My Work" section so it's
      synthesis, not summary.

    ## Operations (see SKILL.md for detail)

    - **Ingest:** raw/ → wiki/. Read source, create/update page, link.
    - **Query:** search wiki/ first; only fall back to raw/ if needed.
    - **Lint:** periodic — orphan pages, missing cross-refs, stale claims.
    """
)


DEFAULT_WIKI_INDEX_MD = dedent(
    """\
    # Wiki Index

    Catalog of wiki pages. Update on every ingest.

    ## Entities

    (none yet)

    ## Concepts

    (none yet)

    ## Topics

    (none yet)
    """
)


DEFAULT_WIKI_LOG_MD = dedent(
    """\
    # Wiki Log

    Chronological record of wiki operations. Append on every ingest / lint.

    Format:
    ```
    YYYY-MM-DD — <operation>: <file(s) affected>
    ```
    """
)


DEFAULT_IDENTITIES_YAML = dedent(
    """\
    # Operator-managed identity reconciliation (FUTURE_WORK §6.1).
    #
    # Each person has a canonical id and a list of platform aliases.
    # When messages arrive with these aliases as authors, the resolver
    # maps them to the canonical so cross-channel pull works across
    # platforms (Alice on Slack pulls her Discord public history, etc.).
    #
    # Add entries as you learn cross-platform identities. The agent
    # doesn't write this file — only operators and the (future)
    # `mimir identities` CLI do.
    #
    # Schema:
    #
    # people:
    #   - canonical: alice                    # short id used as the matching key
    #     display_name: Alice Smith           # optional; for prompt rendering
    #     aliases:
    #       - slack-U123ABC                   # Slack user id
    #       - discord-456789                  # Discord numeric id
    #       - bsky:alice.bsky.social          # Bluesky handle
    #       - email:alice@example.com         # email address
    #     notes: Eng team lead                # optional; surfaces in prompt
    #
    # Alias prefix conventions (informational — resolver treats aliases
    # as opaque strings, so the prefix is for readability only):
    #   slack-<id>      hyphen, alphanumeric id
    #   discord-<id>    hyphen, numeric id
    #   bsky:<handle>   colon (handle contains dots)
    #   email:<addr>    colon (address contains @ and dots)
    #
    # Operators can disable cross-platform pull entirely (compliance,
    # regulated workflows) by setting MIMIR_CROSS_PLATFORM_PULL=false
    # in .env. The resolver still loads but cross_author_messages
    # falls back to direct equality.

    people: []
    """
)


def _write_if_missing(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file doesn't exist.

    Returns True if the file was created.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _ensure_env_secure(path: Path) -> None:
    """Tighten ``path`` to 0o600 (owner read-write only).

    Called after any write that puts API keys into ``.env`` files so that
    secrets are not world-readable at the process umask default (0644).
    No-op when the file does not exist (defensive).
    """
    if path.exists():
        path.chmod(0o600)


def _generate_api_key() -> str:
    """A 256-bit URL-safe random token. Roughly 43 chars; safe for shells
    and Docker env files (no quoting, no escaping)."""
    return secrets.token_urlsafe(32)


# Match `MIMIR_API_KEY=<anything>` (line-anchored, with optional leading
# whitespace). The replacement preserves the leading whitespace so an
# operator's indentation in their .env stays intact.
_API_KEY_LINE_RE = re.compile(r"^(\s*)MIMIR_API_KEY\s*=.*$", re.MULTILINE)
_MIMIR_MODEL_SPEC_LINE_RE = re.compile(
    r"^(\s*)MIMIR_MODEL_SPEC\s*=.*$", re.MULTILINE,
)
_ANTHROPIC_BASE_URL_LINE_RE = re.compile(
    r"^(\s*)ANTHROPIC_BASE_URL\s*=.*$", re.MULTILINE,
)
_MIMIR_QUOTA_POLL_LINE_RE = re.compile(
    r"^(\s*)MIMIR_QUOTA_POLL_ENABLED\s*=.*$", re.MULTILINE,
)


def _env_set_var(env_path: Path, var_name: str, value: str, line_re: re.Pattern[str]) -> None:
    """Rewrite the ``var_name`` line in ``env_path`` with ``value``.
    Appends a new line if the var isn't present. Leaves all other lines
    untouched."""
    body = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    new_line = f"{var_name}={value}"
    if line_re.search(body):
        body = line_re.sub(
            lambda m: f"{m.group(1)}{new_line}", body, count=1
        )
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += new_line + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(body, encoding="utf-8")


def _env_get_var(env_path: Path, line_re: re.Pattern[str]) -> str | None:
    """Return the current var value (possibly empty), or None if the var
    isn't set in the file. Empty string means "set but blank"; None means
    "not present at all"."""
    if not env_path.is_file():
        return None
    body = env_path.read_text(encoding="utf-8")
    match = line_re.search(body)
    if not match:
        return None
    line = match.group(0)
    _, _, value = line.partition("=")
    return value.strip()


def _env_set_api_key(env_path: Path, value: str) -> None:
    _env_set_var(env_path, "MIMIR_API_KEY", value, _API_KEY_LINE_RE)


def _env_get_api_key(env_path: Path) -> str | None:
    return _env_get_var(env_path, _API_KEY_LINE_RE)


def setup_home(
    home: Path,
    *,
    embedding: str = DEFAULT_EMBEDDING_PRESET,
    model: str | None = None,
    subscription: bool = False,
) -> dict[str, object]:
    """Scaffold an agent home directory. Returns a status dict for printing.

    ``embedding`` selects the saga.toml ``[embedding]`` preset (one of
    ``EMBEDDING_PRESETS``). Default: voyage (see
    ``DEFAULT_EMBEDDING_PRESET`` for rationale).

    ``model`` is a bare model name (no provider prefix); setup uses
    ``mimir.model_registry.detect_route`` to resolve to the right
    ``MIMIR_MODEL_SPEC`` + any provider-specific env vars. ``None``
    falls back to ``DEFAULT_MODEL_NAME``.

    ``subscription`` declares the deployment is on a subscription
    plan (not pay-per-token). Effect is provider-polymorphic — see
    ``detect_route``.

    Setup always writes the usage monitor env vars matching the
    route's billing mode:

    * subscription routes → ``MIMIR_QUOTA_POLL_ENABLED=1``
    * API routes → ``MIMIR_COST_HOURLY_LIMIT_USD=5.0`` (sane default
      for per-turn cost-tracker alerts; operators tune post-setup).
    """
    from ..model_registry import detect_route
    route = detect_route(model, subscription=subscription)
    # The runtime (``mimir run`` → ``Config.from_env``) reads
    # MIMIR_MODEL_SPEC from the process environment (compose.env in
    # docker, or an exported var) — it does NOT load <home>/.env, and it
    # ignores this --model/default once the env var is set. So when the
    # env var is present, THAT is the agent's real model: report it in
    # the setup banner (deriving provider/billing via the same
    # detect_route), or the banner contradicts what ``mimir run`` does.
    # setup still scaffolds <home>/.env from ``route`` (the --model /
    # default) as a template — separate from what the runtime resolves.
    # (chainlink #297)
    env_spec = os.environ.get("MIMIR_MODEL_SPEC", "").strip()
    effective_route = detect_route(env_spec) if env_spec else route
    model_spec_from_env = bool(env_spec)
    home = home.resolve()
    if home.exists() and not home.is_dir():
        raise ValueError(
            f"--home {home} exists and is not a directory; refusing to scaffold over it."
        )
    home.mkdir(parents=True, exist_ok=True)

    # First-ever setup? Decide BEFORE the mkdir loop below creates
    # ``memory/core``. A fresh home (no core blocks yet) gets the
    # onboarding bootstrap (init block) seeded after the core templates;
    # an established home never does — the onboarding skill deletes the
    # block when it's done, and re-seeding it would re-trigger onboarding.
    _core_dir = home / "memory" / "core"
    fresh_home = not _core_dir.is_dir() or not any(_core_dir.glob("*.md"))

    created_dirs: list[str] = []
    for sub in (
        "logs",
        "memory/core",
        "memory/channels",
        "memory/issues",
        "prompts",
        "state",
        "state/raw",
        "state/wiki",
        "state/wiki/entities",
        "state/wiki/concepts",
        "state/wiki/topics",
        "messages",
        ".claude/agents",
        "skills",
        "scratch",  # ephemeral writable workspace (gitignored; chainlink #299)
    ):
        p = home / sub
        if not p.exists():
            created_dirs.append(sub)
        p.mkdir(parents=True, exist_ok=True)

    files_created: list[str] = []
    api_key_action: str | None = None
    env_was_new = _write_if_missing(home / ".env", DEFAULT_ENV_TEMPLATE)
    if env_was_new:
        files_created.append(".env")
    # Inject the resolved model spec + provider env vars. Preserves any
    # existing operator value on re-run (don't clobber if the operator
    # already filled it in). This is idempotent in the same shape as
    # the API key generation below.
    if (_env_get_var(home / ".env", _MIMIR_MODEL_SPEC_LINE_RE) or "") == "":
        _env_set_var(
            home / ".env", "MIMIR_MODEL_SPEC", route.model_spec,
            _MIMIR_MODEL_SPEC_LINE_RE,
        )
    # Provider-specific env (e.g., ``ANTHROPIC_BASE_URL`` for Minimax /
    # Moonshot routed deployments). Same idempotency: only write when
    # the line is empty.
    for var_name, var_value in route.env.items():
        # Use a per-var regex by-name so this generalizes to any future
        # provider that adds a different env var (OPENAI_BASE_URL, etc.).
        line_re = re.compile(
            rf"^(\s*){re.escape(var_name)}\s*=.*$", re.MULTILINE,
        )
        if (_env_get_var(home / ".env", line_re) or "") == "":
            _env_set_var(home / ".env", var_name, var_value, line_re)
    # Usage-monitor env vars matching the route's billing mode:
    # subscription routes get the quota-poller-enabled flag; API
    # routes get a default per-turn cost ceiling. Always written
    # (no opt-in flag) because the right monitor for the chosen
    # billing model should just work. Idempotent: don't clobber an
    # operator-set value on re-run.
    for var_name, var_value in route.monitor_env.items():
        line_re = re.compile(
            rf"^(\s*){re.escape(var_name)}\s*=.*$", re.MULTILINE,
        )
        existing = _env_get_var(home / ".env", line_re)
        # ``None`` = key missing; empty string = key present but blank.
        # Both treated as "no operator override yet" → write our value.
        # Non-empty non-default values stay untouched.
        if existing in (None, "", "0", "0.0"):
            _env_set_var(home / ".env", var_name, var_value, line_re)
    monitor_status = effective_route.monitor_label or "no monitor configured"

    # Generate a fresh MIMIR_API_KEY on first setup (or if the operator
    # left the value blank). Existing non-empty keys are preserved on
    # re-run — operators can rotate via `mimir regenerate-api-key`.
    if (_env_get_api_key(home / ".env") or "") == "":
        _env_set_api_key(home / ".env", _generate_api_key())
        api_key_action = "generated"
    # saga runs in-process and uses no key, so no SAGA_API_KEY is generated.
    # Nothing secret is written to .env or the tracked saga.toml.

    # Tighten .env to 0o600 (owner read-write only) after all secrets are
    # written. The file lands at the process umask default (typically 0644,
    # world-readable) otherwise — leaking MIMIR_API_KEY to any local user on a
    # multi-tenant host.
    _ensure_env_secure(home / ".env")

    # v0.5 §2: write saga.toml for in-process saga (skip if --no-saga; the
    # caller passes that signal by setting saga_key to None — but for now
    # setup always generates one).
    (home / ".mimir").mkdir(parents=True, exist_ok=True)
    if _write_if_missing(
        home / "saga.toml",
        _default_saga_toml(home, embedding=embedding),
    ):
        files_created.append("saga.toml")
    # scheduler.yaml + prompts/ seed from the bundled templates (the
    # canonical sources). seed_scheduler/seed_prompts return per-file
    # status so we can update files_created accurately.
    from ..skill_defs import seed_scheduler as _seed_scheduler
    from ..prompt_templates import seed_prompts as _seed_prompts_pre
    if _seed_scheduler(home) == "created":
        files_created.append("scheduler.yaml")
    for _pname, _pstatus in _seed_prompts_pre(home).items():
        if _pstatus == "created":
            files_created.append(f"prompts/{_pname}")
    for _cname, _cstatus in seed_core_memory(home).items():
        if _cstatus == "created":
            files_created.append(f"memory/core/{_cname}")
    # Onboarding bootstrap — only on a brand-new home (see fresh_home
    # above). Drives the agent to the onboarding skill on first contact;
    # it deletes the block when onboarding completes, so it self-cleans
    # and is never re-seeded on later runs.
    if fresh_home and seed_init_block(home) == "created":
        files_created.append(f"memory/core/{INIT_BLOCK_NAME}")
    if _write_if_missing(home / "state" / "wiki" / "AGENTS.md", DEFAULT_WIKI_AGENTS_MD):
        files_created.append("state/wiki/AGENTS.md")
    if _write_if_missing(home / "state" / "wiki" / "index.md", DEFAULT_WIKI_INDEX_MD):
        files_created.append("state/wiki/index.md")
    if _write_if_missing(home / "state" / "wiki" / "log.md", DEFAULT_WIKI_LOG_MD):
        files_created.append("state/wiki/log.md")
    if _write_if_missing(home / "state" / "identities.yaml", DEFAULT_IDENTITIES_YAML):
        files_created.append("state/identities.yaml")
    if _write_if_missing(
        home / "state" / "heartbeat-backlog.md", DEFAULT_HEARTBEAT_BACKLOG
    ):
        files_created.append("state/heartbeat-backlog.md")
    if _write_if_missing(
        home / "memory" / "issues" / "README.md",
        DEFAULT_ISSUES_README,
    ):
        files_created.append("memory/issues/README.md")
    if _write_if_missing(
        home / "state" / "proposed-changes.md", DEFAULT_PROPOSED_CHANGES
    ):
        files_created.append("state/proposed-changes.md")

    seeded_subagents = seed_subagent_defs(home)
    # Migrate legacy ``.claude/skills/`` → ``skills/`` first, then
    # refresh the bundled built-ins into ``.mimir_builtin_skills/``.
    # Prompts and scheduler.yaml were already seeded above (idempotent).
    from ..skill_defs import migrate_legacy_skills_dir
    migrate_legacy_skills_dir(home)
    seeded_skills = seed_skills(home)

    # PR 4b: bootstrap the home dir as a git repo (idempotent). Reads
    # ``MIMIR_STATE_REPO`` + ``GITHUB_TOKEN`` from the environment so a
    # fresh clone-on-init works when the operator's wired the .env
    # before running setup. Skipped when MIMIR_GIT_TRACKING_ENABLED is
    # set explicitly to a falsy value — otherwise bootstrap is the
    # default once 4b lands.
    #
    # CR2 (ops & observability) fix: use ``_env_bool`` from config.py
    # so the truthy/falsy interpretation matches Config.from_env's
    # parsing exactly. Pre-fix this used a bespoke 4-token list
    # (``{"false", "0", "no", "off"}``) while config.py uses
    # ``_env_bool`` (truthy = ``1/true/yes/on``, default for anything
    # else). For e.g. ``MIMIR_GIT_TRACKING_ENABLED=enabled`` or ``=y``
    # the two parsers disagreed: setup interpreted as enabled (not
    # in the falsy list), Config interpreted as disabled (not in the
    # truthy list). Now both use the same canonical parser.
    from ..config import _env_bool
    git_bootstrap_status: dict[str, object] | None = None
    if _env_bool("MIMIR_GIT_TRACKING_ENABLED", True):
        try:
            from ..git_bootstrap import bootstrap_git_repo
            br = bootstrap_git_repo(
                home,
                state_repo=os.environ.get("MIMIR_STATE_REPO"),
                github_token=os.environ.get("GITHUB_TOKEN"),
            )
            git_bootstrap_status = {
                "initialized": br.initialized,
                "cloned": br.cloned,
                "pulled": br.pulled,
                "pull_blocked": br.pull_blocked,
                "bootstrap_commit": br.bootstrap_commit,
                "gitignore_written": br.gitignore_written,
                "hook_written": br.hook_written,
                "remote_configured": br.remote_configured,
                "credentials_written": br.credentials_written,
                "legacy_token_url_migrated": br.legacy_token_url_migrated,
                "upstream_set": br.upstream_set,
                "initial_push": br.initial_push,
            }
        except Exception as exc:  # noqa: BLE001
            # Bootstrap failures shouldn't block ``mimir setup`` — the
            # operator can re-run after fixing the env. Surface the
            # error in the printed report.
            git_bootstrap_status = {"error": str(exc)}

    return {
        "home": str(home),
        "dirs_created": created_dirs,
        "files_created": files_created,
        "subagents": seeded_subagents,
        "skills": seeded_skills,
        "api_key_action": api_key_action,
        "git_bootstrap": git_bootstrap_status,
        "embedding_preset": embedding,
        "model_spec": effective_route.model_spec,
        "provider_name": effective_route.provider_name,
        "billing_mode": effective_route.billing_mode,
        "monitor_status": monitor_status,
        # When MIMIR_MODEL_SPEC is set in the environment the three fields
        # above reflect it (the runtime's real model) rather than the
        # --model/default route scaffolded into <home>/.env. (chainlink #297)
        "model_spec_from_env": model_spec_from_env,
        "setup_default_spec": route.model_spec,
    }


def _skill_env_summary(home: str) -> list[dict]:
    """Return env-dep info for installed skills that declare an ``env:`` block.

    Scans ``<home>/.mimir_builtin_skills/`` and ``<home>/skills/`` for
    SKILL.md files with ``env:`` frontmatter blocks. Returns a list of
    dicts — one per skill that has at least one required or optional var:

    .. code-block:: python

        [
            {
                "name": "weather",
                "required": [
                    {"name": "OPENWEATHER_API_KEY", "description": "...",
                     "example": "...", "set": False},
                ],
                "optional": [],
            },
        ]

    ``"set"`` is ``True`` when the var is non-empty in the current
    ``os.environ`` (i.e. already exported before ``mimir setup`` ran).

    Operator-installed skills in ``skills/`` shadow same-named builtins so
    each skill name appears at most once in the result.

    Errors in a single SKILL.md are silently skipped — one bad file does
    not abort the whole scan.
    """
    from ..skill_md import parse_env_block

    home_path = Path(home)
    seen: set[str] = set()
    result: list[dict] = []

    # Operator-placed skills shadow builtins; process operator dir FIRST so
    # ``seen`` prevents the builtin copy from overriding operator config.
    roots = [
        home_path / "skills",
        home_path / ".mimir_builtin_skills",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name
            if name in seen:
                continue  # shadowed by earlier (higher-priority) copy
            skill_md_path = skill_dir / "SKILL.md"
            if not skill_md_path.exists():
                continue
            # Mark as seen BEFORE env-block check so operator skills
            # (processed first) suppress same-named builtins even when
            # the operator copy has no env: block.
            seen.add(name)
            try:
                text = skill_md_path.read_text()
                req, opt = parse_env_block(text)
            except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
                log.debug("_skill_env_summary: skipping %s: %s", skill_md_path, exc)
                continue
            if not req and not opt:
                continue

            def _augment(specs: list[dict]) -> list[dict]:
                return [
                    {**s, "set": bool(os.environ.get(s["name"]))}
                    for s in specs
                ]

            result.append({
                "name": name,
                "required": _augment(req),
                "optional": _augment(opt),
            })

    return result


def _print_setup_report(status: dict[str, object]) -> None:
    home = status["home"]
    print(f"mimir home ready at: {home}")
    if status["dirs_created"]:
        print(f"  created dirs:  {', '.join(status['dirs_created'])}")  # type: ignore[arg-type]
    if status["files_created"]:
        print(f"  wrote files:   {', '.join(status['files_created'])}")  # type: ignore[arg-type]
    skills = status["skills"]
    subs = status["subagents"]
    if isinstance(skills, dict):
        new_skills = sorted(n for n, s in skills.items() if s == "created")
        if new_skills:
            print(f"  skills seeded: {', '.join(new_skills)}")
    if isinstance(subs, dict):
        new_subs = sorted(n for n, s in subs.items() if s == "created")
        if new_subs:
            print(f"  subagents seeded: {', '.join(new_subs)}")
    if status.get("api_key_action") == "generated":
        print("  MIMIR_API_KEY:  generated (see .env; rotate via `mimir regenerate-api-key`)")
    # Model + usage-monitor routing — surface what setup decided so
    # the operator can confirm or override.
    model_spec = status.get("model_spec")
    provider = status.get("provider_name")
    billing_mode = status.get("billing_mode")
    if model_spec:
        print(
            f"  model spec:    {model_spec}   "
            f"(provider: {provider}; billing: {billing_mode})"
        )
        # Surface the pip extra this model's chat adapter needs (chainlink
        # #292) so the operator installs it now rather than hitting an
        # ImportError on first run. claude-code is git-installed (no extra)
        # and is covered by the LLM-auth steps printed below.
        from ..providers import extra_for_spec
        adapter_extra = extra_for_spec(model_spec)
        if adapter_extra:
            print(f"  model adapter: pip install mimir-agent[{adapter_extra}]")
        if status.get("model_spec_from_env"):
            print(
                "                 ↑ from MIMIR_MODEL_SPEC in the environment "
                "(e.g. compose.env) — this is what `mimir run` uses;"
            )
            print("                   <home>/.env is NOT read at runtime.")
            _default_spec = status.get("setup_default_spec")
            if _default_spec and _default_spec != model_spec:
                print(
                    f"                   (setup --model/default would be: "
                    f"{_default_spec})"
                )
    monitor_status = status.get("monitor_status")
    if monitor_status:
        print(f"  usage monitor: {monitor_status}")
    git_st = status.get("git_bootstrap")
    if isinstance(git_st, dict):
        if "error" in git_st:
            print(f"  git bootstrap:  FAILED — {git_st['error']}")
        else:
            actions: list[str] = []
            if git_st.get("cloned"):
                actions.append("cloned remote")
            elif git_st.get("initialized"):
                actions.append("init'd repo")
                if git_st.get("bootstrap_commit"):
                    actions.append("seeded commit")
            else:
                actions.append("repo present")
            if git_st.get("pulled"):
                actions.append("pulled --ff-only")
            elif git_st.get("pull_blocked"):
                actions.append("pull BLOCKED (operator review needed)")
            if git_st.get("remote_configured"):
                actions.append("remote=origin")
            if git_st.get("gitignore_written"):
                actions.append(".gitignore installed")
            if git_st.get("hook_written"):
                actions.append("pre-commit hook installed")
            if git_st.get("credentials_written"):
                actions.append("credential helper installed")
            if git_st.get("legacy_token_url_migrated"):
                actions.append("legacy in-URL token stripped")
            if git_st.get("initial_push"):
                actions.append("initial push (created remote main)")
            elif git_st.get("upstream_set"):
                actions.append("upstream tracking set")
            print(f"  git bootstrap:  {' / '.join(actions)}")
    print()
    print("Recurring scheduled tasks (active when `mimir run` starts):")
    print("  LLM ticks (scheduler.yaml):")
    print("    - heartbeat:           hourly (autonomous-work cadence)")
    print("    - reflect:             Sun 06:00 UTC (cross-session audit)")
    print("  Non-LLM crons (auto-registered by the runtime):")
    print("    - saga-consolidate:    nightly 04:00 UTC (atom merge / synthesis)")
    print("    - introspection-report: Fri 14:00 UTC (behavioral / health snapshot)")
    print("  Override any cadence via env vars or scheduler.yaml.")
    print()
    print("Next steps:")
    print(f"  1. Configure LLM auth — pick one:")
    print(f"     a. Max plan (free):  claude setup-token")
    print(f"        (or `claude login` for an interactive session — same effect.)")
    print(f"     b. Anthropic API:    set ANTHROPIC_API_KEY in {home}/.env")
    print(f"     c. Gateway (e.g. LiteLLM, OpenRouter):")
    print(f"        set ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN in .env")
    # Embedding-provider-specific guidance — the saga.toml mimir setup
    # generated has provider=<preset>, so the step here surfaces the
    # matching env var. Falls back to fastembed automatically if the
    # key is unset (saga.embeddings.get_provider auto-fallback).
    preset = status.get("embedding_preset", DEFAULT_EMBEDDING_PRESET)
    if preset == "voyage":
        print(f"  2. (optional) set VOYAGE_API_KEY in .env for saga's embeddings")
        print(f"     (voyage-4-lite — $0.02/1M tokens, 200M signup free credit).")
        print(f"     Leave blank to fall back to local fastembed (no API needed).")
    elif preset == "openai":
        print(f"  2. (optional) set OPENAI_API_KEY in .env for saga's embeddings;")
        print(f"     leave blank to fall back to local fastembed (no API needed).")
    else:  # fastembed
        print(f"  2. saga embeddings configured for local fastembed —")
        print(f"     no API key needed. First run downloads the ~33MB ONNX model.")
    print(f"  3. (optional) Edit {home}/memory/core/00-identity.md")
    print(f"  4. Run:  mimir run --home {home}")
    # Passive skill env-deps summary — non-blocking, informational only.
    # Lists skills with env: blocks so the operator knows what to configure.
    # No prompts here (setup runs non-interactively from Dockerfiles / scripts).
    # Phase 3 (chainlink #211) will add `mimir skills configure <name>` for the
    # interactive flow; this block is the discovery surface that makes the gap visible.
    env_deps = _skill_env_summary(str(home))
    if env_deps:
        print()
        print("Skill env-var status (configure with `mimir skills configure <name>`):")
        for sk in env_deps:
            req = sk["required"]
            opt = sk["optional"]
            missing_req = [v["name"] for v in req if not v["set"]]
            set_req = [v["name"] for v in req if v["set"]]
            unset_opt = [v["name"] for v in opt if not v["set"]]
            if missing_req:
                label = f"required (not set): {', '.join(missing_req)}"
            elif set_req:
                if unset_opt:
                    label = f"configured ✓ (optional unset: {', '.join(unset_opt)})"
                else:
                    label = "configured ✓"
            elif unset_opt:
                label = f"optional (not set): {', '.join(unset_opt)}"
            else:
                label = "configured ✓"
            print(f"  {sk['name']:20s} {label}")


def regenerate_api_key(home: Path) -> str:
    """Rewrite ``<home>/.env``'s MIMIR_API_KEY line with a fresh random
    value. Returns the new key. Other env vars are left untouched.

    CR2 (ops & observability) fix: refuse to scaffold a fresh ``.env``
    in a home that has never been ``mimir setup``-ed. Pre-fix this
    function created ``<home>/.env`` if missing — so a typo'd home
    path (``regenerate_api_key("/tmp/typo")``) would silently produce
    a one-line .env in an unrelated directory. The CLI front-door has
    a check; importing the function from elsewhere bypassed it.
    """
    home = home.resolve()
    env_path = home / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(
            f"{env_path} does not exist. Run 'mimir setup --home "
            f"{home}' first to scaffold the home directory before "
            f"regenerating the API key."
        )
    new_key = _generate_api_key()
    _env_set_api_key(env_path, new_key)
    _ensure_env_secure(env_path)
    return new_key


# ---------------------------------------------------------------------------
# Argparse registration (extracted to commands.setup so cli.py stays lean)
# ---------------------------------------------------------------------------


def add_argparse(sub: "argparse._SubParsersAction") -> "argparse.ArgumentParser":  # type: ignore[name-defined]
    """Register ``mimir setup`` subcommand parser.  Returns the created parser."""
    import argparse as _ap
    setup_p = sub.add_parser(
        "setup",
        help="Scaffold a mimir home (dirs, .env, scheduler.yaml, skills, subagents).",
    )
    setup_p.add_argument(
        "--home", type=Path, default=Path.cwd(),
        help="Target directory (default: current working dir).",
    )
    setup_p.add_argument(
        "--embedding", type=str, default=DEFAULT_EMBEDDING_PRESET,
        choices=list(EMBEDDING_PRESETS),
        help=(
            f"Embedding provider preset for the generated saga.toml "
            f"(default: {DEFAULT_EMBEDDING_PRESET}). Voyage requires "
            f"VOYAGE_API_KEY; openai requires OPENAI_API_KEY; "
            f"fastembed is fully local. saga's [consolidation] "
            f"similarity_threshold automatically tunes to the matching "
            f"value (0.92 for voyage/fastembed, 0.80 for openai)."
        ),
    )
    setup_p.add_argument(
        "--model", type=str, default=None,
        help=(
            "Bare model name (no provider prefix needed). Setup "
            "auto-routes based on the name: ``MiniMax-M2.7`` → Minimax "
            "(via Anthropic-compat endpoint); ``kimi-k2-*`` → "
            "Moonshot; ``gpt-*`` / ``o[1-4]-*`` → OpenAI; ``claude-*`` "
            "→ direct Anthropic API. Generates the right "
            "``MIMIR_MODEL_SPEC`` + ``ANTHROPIC_BASE_URL`` entries in "
            ".env. Also wires the usage monitor that matches the "
            "provider's billing model — subscription routes get quota "
            "polling; API routes get per-turn cost tracking with a "
            "default $/hr ceiling. Default model: claude-sonnet-4-6 "
            "via direct API. See ``mimir/model_registry.py`` for the "
            "full mapping."
        ),
    )
    setup_p.add_argument(
        "--subscription", action="store_true",
        help=(
            "Declare this deployment runs on a subscription plan for "
            "the chosen provider (not pay-per-token API billing). "
            "Effect is provider-polymorphic: Claude family swaps to "
            "``claude-code:`` (Max OAuth via subprocess — the "
            "protocol IS different); OpenAI / Minimax / Moonshot keep "
            "the same model_spec (same HTTP endpoint, just a "
            "different API token tier). Either way the usage monitor "
            "flips from cost-tracking to quota-polling. Without this "
            "flag, every route defaults to pay-per-token + cost "
            "monitoring."
        ),
    )
    return setup_p
