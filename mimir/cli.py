"""Command-line entrypoint for mimir.

Subcommands:
- ``mimir setup [--home DIR]`` — scaffold an agent home (dirs, .env template,
  scheduler.yaml stub, skills, subagent defs). Idempotent — never overwrites
  existing files.
- ``mimir run [--home DIR]``   — run the server (default if no subcommand).
- ``mimir identities {list,add,remove,resolve}`` — manage identity
  reconciliation entries (FUTURE_WORK §6.1).

Both run/setup commands export ``MIMIR_HOME`` to the resolved path before
loading ``Config.from_env()``, so the CLI flag and the env var converge.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
from pathlib import Path
from textwrap import dedent
from typing import Sequence

import yaml

from .identities import IdentityResolver
from .skill_defs import seed_skills
from .subagent_defs import seed_subagent_defs


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

    # ---- SAGA sidecar (memory) -------------------------------------------
    SAGA_ENDPOINT=http://localhost:3002
    SAGA_API_KEY=
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
    # dispatch failures). Leave blank to disable. Use a normal channel_id —
    # typically your DM with the bot, e.g. dm-slack-U05XXXX or dm-discord-NNN.
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


DEFAULT_HEARTBEAT_PATTERNS = dedent(
    """\
    <!-- desc: what works (and what doesn't) during heartbeat ticks -->
    # Heartbeat Patterns

    Append observations from your heartbeat experience — tasks that
    fit well, ones that didn't, time-of-day patterns, mistakes worth
    not repeating. Keep it tight; this block is in core memory.

    ## Multi-item ticks (when one finishes fast)

    Default is "pick ONE item per tick" — but that produces an
    artificial ceiling on ticks where the picked item happened to be
    tightly bounded (a quick audit, a one-edit doc reconciliation).
    The prompt cost is sunk regardless; exiting early wastes capacity.

    Relaxation: **when the first item finishes in <10 min and the
    next ready item is a natural successor, pick a second item rather
    than exiting.** Cap at 2 items per tick; cap at 30 min wall-clock
    so the next tick doesn't get behind.

    "Natural successor" examples:
    - next subissue in the same chainlink chain (when it's unblocked
      and bounded)
    - another single-edit backlog item from
      `state/heartbeat-backlog.md`
    - a propose-only draft that pairs with the just-completed work

    If the first item produced something that needs operator review
    before its natural successor can run, **do not exit** — surface
    and pivot. See §"Surfacing operator-attention items" and
    §"Reading backlog as the operator-gated fallback" below.

    ## Surfacing operator-attention items

    When heartbeat work reaches a point that needs operator attention
    — a draft requiring approval, a per-file migration list whose
    decisions load-bear, an overlap-pair resolution, a propose-only
    doc whose recommendations gate the next phase — **send a message
    to the operator channel before exiting or pivoting.** Heartbeats
    are silent by default but operator-gates are exactly the
    surface-it case; silently exiting leaves the operator to discover
    the gate next time they think to look.

    Message shape (tight, no preamble, no decoration):
    - One line: what was completed + path to the artifact.
    - One line: the load-bearing decision(s) the operator should
      sanity-check. Don't dump the whole doc — point at the section.
    - One line: what the heartbeat is pivoting to (or "exiting,
      reading-backlog empty for this slot").

    Channel: route through the operator alert channel (when
    configured) or the active operator chat channel.

    ## Reading backlog as the operator-gated fallback

    When current chainlink work hits an operator-gate, the reading
    backlog is the canonical pivot — **not exiting**. Reading work
    is the right shape because:
    - **Ungated** — no decisions required to start.
    - **Bounded** — one source per tick (or one chunk if the source
      is too big), 30-min wall-clock cap holds.
    - **Non-output-producing in a decision sense** — synthesis lands
      as wiki pages which don't ask the operator for anything.

    Curate reading-backlog items in `state/heartbeat-backlog.md`
    under a clearly-marked "Reading backlog" section.

    Pivot precedence when current work is operator-gated:
    1. Reading-backlog item (in priority order).
    2. Librarian / propose-only audit (no-decision-output work).
    3. Exit silently if neither is available — but log this in the
       heartbeat result so the operator notices.

    Wall-clock and cost caps apply to the pivoted-to item the same
    way they apply to the primary item; the 30-min cap is for the
    whole tick, not per-item.
    """
)


# The bundled heartbeat.md / reflect.md under mimir/prompt_templates/
# are the canonical scheduled-tick prompts; setup_home seeds them via
# seed_prompts(). The short "trigger" constants that used to live here
# (DEFAULT_HEARTBEAT_PROMPT / DEFAULT_REFLECT_PROMPT) were a relic of
# when heartbeat/reflection were bundled SKILLS — the agent would
# receive a 5-line trigger and dispatch to the skill. Post-unbundling
# the scheduler loads the full workflow body directly.


DEFAULT_VSM_TERMS = dedent(
    """\
    <!-- desc: VSM (Viable System Model) terms used in mimir's prompt blocks -->
    # VSM Terminology

    Mimir's prompt blocks use Beer's Viable System Model vocabulary. You
    don't need to manage these levels — they're how mimir's own internals
    are organized — but the prompt surfaces some of them, so it's useful
    to know what each label means when you see it.

    ## The five systems

    - **S1 — operations.** The thing actually doing the work in a given
      moment. For mimir: each tool call, each turn's reply.
    - **S2 — coordination.** Stops adjacent S1 work from colliding. For
      mimir: the dispatcher's per-channel queues, the loop detector that
      catches send_message duplicates.
    - **S3 — control / here-and-now.** User-driven work, the inside-now
      view. For mimir: turns triggered by ``user_message`` events.
    - **S4 — intelligence / there-and-then.** Autonomous, future-looking
      work. For mimir: scheduled ticks (heartbeats, decay+consolidate
      cron, reflection, introspection report).
    - **S5 — identity / policy.** Persona, conventions, values that
      arbitrate when S3 and S4 conflict. For mimir: this core memory
      block, ``00-persona.md``, ``30-reflection-policy.md``.

    ## The algedonic channel

    Pain / pleasure signals that bypass the regulatory hierarchy and
    feed back to S5. For mimir: events.jsonl errors, denials, loop
    hits, react_received reactions. Surfaced in the turn prompt as the
    "Recent feedback signals" block — algedonic in (negatives) and
    algedonic out (positives) are both there.

    ## Phrases you may see in prompt blocks

    - **S3-S4 share** (in ``## Self-state``) — what fraction of the
      24h tool-call budget went to user-driven (S3) vs scheduled (S4)
      work. Informational; the homeostat doesn't suppress on this
      anymore (review #7), but it tells you whether your day skewed
      reactive or autonomous.
    - **S3-star** — "the aggregate over all S3 work this period"
      (e.g., reflection's behavioral track is S3-star: looking back
      across all reactive turns of the week).
    - **Algedonic surfacing** — anything that lifts a signal *past*
      the normal regulatory loops because it was painful or
      pleasurable enough to deserve direct attention.

    Beer's full framework has more terms (channels of variety,
    operational vs metasystem, recursion); the above is what mimir
    actually uses in prompts and code comments. Don't write the
    framework into chat replies — it's internal scaffolding.
    """
)


DEFAULT_REFLECTION_POLICY = dedent(
    """\
    <!-- desc: which reflection actions are autonomous vs propose-only -->
    # Reflection Policy

    Read by the reflection skill at the start of every weekly audit.
    Edit this file to widen or tighten the autonomous boundary as
    trust builds. Conservative defaults:

    ## Autonomous (the reflection turn may apply directly)

    - SAGA atom decay calls
    - SAGA triples linking (additive)
    - Append-only edits to memory/core/40-learned-behaviors.md
    - Wiki orphan tagging (writes to state/wiki/index.md — flag, don't delete)

    ## Propose-only (write to state/proposed-changes.md, operator reviews)

    - Core memory edits (cleanup, restructure, promote-to-core, demote)
    - Persona block edits (memory/core/00-*.md)
    - Skill creation (skills/<name>/)
    - Wiki page deletions
    - Memory file deletions

    If this file is missing or unparseable, fall back to propose-only
    for everything — never auto-apply when in doubt.
    """
)


DEFAULT_LEARNED_BEHAVIORS = dedent(
    """\
    <!-- desc: behaviors learned through reflection - autonomous additions only -->
    # Learned Behaviors

    Append-only. The reflection turn writes here when it observes a
    pattern worth keeping (a recurring approach that worked, a
    failure mode worth avoiding, a heuristic that emerged across
    several sessions). Never edit prior entries from a reflection —
    propose any restructure via state/proposed-changes.md instead.

    Format per entry:

    ```
    ## YYYY-MM-DD — short title
    What I noticed: ...
    What works: ...
    Trigger: <when this applies>
    ```
    """
)


DEFAULT_FILING_RULES = dedent(
    """\
    <!-- desc: where things go in memory/ and state/, with severity for misfiles -->
    # Filing Rules

    Where to put a thing in `memory/` or `state/`. The reader's a future-mimir
    noticing something might be misfiled in the wild; this block tells them
    how urgent the cleanup is and what the right home looks like.

    ## Severity rubric

    - **cosmetic** — looks wrong but no functional impact. Reader still
      finds the content. Cleanup is opportunistic.
    - **drift-amplifier** — accumulates over time, degrades discoverability.
      Each individual misfile is small; aggregate damage is real. Cleanup
      is worth doing periodically.
    - **system-breaking** — breaks an invariant. Per-turn prompt loses an
      essential block, an auto-indexer breaks, or a writer/reader contract
      violates. Cleanup is immediate.

    ## Layers — `memory/` (in the per-turn prompt)

    - **`memory/core/`** — always-in-context. Persona, voice, conventions,
      terminology, reflection-policy, hard-won heuristics. Numeric prefix
      governs ordering. Each block earns its prompt-cost on every turn.
      Session-scoped notes, candidate learnings, raw source material → NOT
      here.
      *Severity if misfiled into core: system-breaking (prompt inflation).*
    - **`memory/channels/<id>/`** — per-channel facts. Operator name,
      preferences, channel-specific patterns. Cross-channel content goes
      elsewhere.
      *Severity if misfiled: drift-amplifier (channel injection misses it).*
    - **`memory/issues/`** — operational-gotcha fingerprints. Failure-mode
      notes, infra gotchas, runbook-shaped entries. Each entry surfaces in
      the every-turn `memory/INDEX.md` description list — its purpose is
      hash-lookup against a future symptom. Concept-level synthesis →
      `state/wiki/concepts/` instead.
      *Severity if misfiled: drift-amplifier (INDEX bloats or gotcha
      re-discovered from scratch).*
    - **`memory/learnings-pending.md`** — append-only buffer for candidate
      learned behaviors. Reflection promotes durable ones to
      `core/40-learned-behaviors.md`. Synthesis turns capture here, NOT
      direct-to-core.
    - **`memory/INDEX.md`** — auto-managed; hand-edits overwritten. The
      convention to enforce is the per-file `<!-- desc: ... -->` first-line.

    ## Layers — `state/` (outside the prompt, file_search reachable)

    - **`state/wiki/concepts/`** — concept-level synthesis from raw source
      ingest. Pattern frameworks, theoretical models, named patterns. Each
      page typically has thesis / framework / mimir-mapping /
      Skepticism-or-open-critiques.
    - **`state/wiki/topics/`** — long-form map-of-territory writeups
      (typically >5 KB). Baseline analyses, runner architectures,
      benchmark layouts.
    - **`state/wiki/entities/`** — people / projects / repos. Entity pages
      surfaced when their work recurs as a source.
    - **`state/wiki/{AGENTS,index,log}.md`** — wiki meta. AGENTS = ingest
      conventions, index = curated table of contents, log = append-only
      ingest log.
    - **`state/raw/`** — verbatim source preservation. Filename pattern
      `YYYY-MM-DD-<source>.md`, provenance header at top. **Append-only**:
      write once, never edit. Only state/ layer with hard immutability.
    - **`state/spec/`** — design docs in flight (chainlink-tracked). Lives
      during implementation. **Post-merge**: archive under
      `state/spec/archive/` (historical) or promote to `state/wiki/topics/`
      (reusable architecture).
    - **`state/proposed-changes.md`** — operator-review queue. Append-only
      by mimir; operator marks resolved inline or by deletion.
    - **`state/heartbeat-backlog.md`, `state/identities.yaml`,
      `state/INDEX.md`** — named singletons / operator-managed /
      auto-managed; healthy as-is.

    **Top-level `state/` rule:** nothing lives at top-level `state/` except
    auto-meta (INDEX.md), operator-managed yaml (identities.yaml), or named
    singletons with explicit purpose (heartbeat-backlog.md,
    proposed-changes.md). Free-form top-level state files =
    **drift-amplifier** misfiling.

    ## Two filing questions

    When uncertain, ask one of these binary questions and the answer routes
    you:

    **Q1: "Am I asking the operator to make a decision?"**
    - Yes → `state/proposed-changes.md` (append with date + topic +
      decision-needed framing). A controversial spec that's effectively
      an approval request: write spec to `state/spec/`, then add a
      one-line pointer entry to `state/proposed-changes.md`.
    - No → `state/spec/<feature>-plan.md` (descriptive, "here's the plan").

    **Q2: "Is this an operational issue I might hit, that needs flagging
    in the every-turn `memory/INDEX.md`?"**
    - Yes → `memory/issues/` (fingerprint-shaped, runbook character).
    - No (concept/topic without operational-gotcha shape) → `state/wiki/`.

    ## Misfiling table

    | Pattern | Belongs in | Severity |
    |---------|------------|----------|
    | Free-form file at top-level `state/<name>.md` (not a named singleton) | `state/wiki/topics/` or `state/raw/` | drift-amplifier |
    | Operational gotcha in `state/wiki/concepts/` | `memory/issues/` | drift-amplifier |
    | Concept synthesis in `memory/issues/` | `state/wiki/concepts/` | drift-amplifier |
    | Operator-decision-request in `state/spec/` (no proposed-changes pointer) | `state/proposed-changes.md` (or both, with pointer) | drift-amplifier |
    | Channel-scoped fact in `memory/issues/` or `state/wiki/` | `memory/channels/<id>/` | drift-amplifier |
    | Session-scoped note in `memory/core/` | `memory/learnings-pending.md` or discard | **system-breaking** |
    | Candidate learning written directly to `memory/core/40-learned-behaviors.md` (not by reflection) | `memory/learnings-pending.md` | drift-amplifier |
    | Verbatim source under `state/wiki/` (no provenance header) | `state/raw/<YYYY-MM-DD>-<source>.md` (with synthesis at the wiki layer) | cosmetic |
    | Stub-shaped seed file persists alongside lived-in successor | retire the seed | drift-amplifier |

    ## Lifecycle pointers

    - **Append-only**: `state/raw/`, `state/wiki/log.md`,
      `memory/learnings-pending.md` (capture only — reflection edits via
      promote/drop), `memory/core/40-learned-behaviors.md` (reflection
      writes only).
    - **Edit-in-place**: most other layers — channels, issues, wiki
      concepts/topics/entities, spec docs in flight.
    - **Auto-managed**: `memory/INDEX.md`, `state/INDEX.md`,
      `state/wiki/index.md`. Hand-edits are overwritten end-of-turn.
    """
)


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
EMBEDDING_PRESETS: tuple[str, ...] = ("voyage", "openai", "fastembed", "nvidia-nim")
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
    if preset == "nvidia-nim":
        return dedent(
            """\
            [embedding]
            # NVIDIA NIM nv-embedqa-e5-v5 — saga's historical default.
            # 1024d retrieval-tuned via NIM hosted API. Untested on the
            # 2026-05 LongMemEval bench; kept for legacy compatibility
            # with saga deployments that already use NIM.
            provider = "nvidia-nim"
            url = "https://integrate.api.nvidia.com/v1/embeddings"
            model = "nvidia/nv-embedqa-e5-v5"
            dimensions = 1024
            api_key_env = "NVIDIA_NIM_API_KEY"
            """
        )
    raise ValueError(
        f"unknown embedding preset: {preset!r}. "
        f"valid: {EMBEDDING_PRESETS}"
    )


def _default_saga_toml(
    home: Path,
    api_key: str,
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
      0.80 for openai / nvidia-nim. See saga README for the sweep table.
    - ``[retrieval].enable_contextual_rewrite = true`` — mimir already
      passes ``context=`` on every query; flipping rewrite on means short
      referential queries ("yes, look for that") get resolved before
      retrieval. No-op when context is empty.
    - ``[triples].enable_extraction = true`` — populate the triples table
      on consolidation so future P41-style query-intent-gated work has
      data. ``[retrieval].enable_triple_augment_v2`` stays off (the
      post-fix bench showed -3.7pp multi-session, -2.3pp temporal).
    - ``[server].api_key`` matches the SAGA_API_KEY in mimir's .env so
      flipping to external-saga later doesn't require re-running setup.
      Unused in-process.
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
        # at saga's historical default 0.80), 0.80 for openai and
        # nvidia-nim. See saga README for the full sweep table; swap
        # to a literal float here to override.
        similarity_threshold = "auto"

        [server]
        # Matches SAGA_API_KEY in mimir's .env. Unused in in-process mode;
        # set up-front so flipping to external saga later (i.e., setting
        # SAGA_ENDPOINT to a non-localhost URL) doesn't require re-running
        # setup or rotating keys.
        api_key = "{api_key}"
        """
    )


DEFAULT_PROPOSED_CHANGES = dedent(
    """\
    # Proposed Changes

    Pending HITL items from the reflection skill. Operator reviews on
    their own cadence; once an item is applied or rejected, move it
    to a `## Applied` / `## Rejected` section below or remove it.

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

    The `Predicted effect:` line is what the §12.2 audit pass measures
    against. Phrase it as something the agent could verify by reading
    events.jsonl / turns.jsonl: error-rate delta, tool-call frequency
    delta, etc. When the operator merges a proposal, they run
    `mimir reflection mark-applied "<heading substring>"` to move it
    here and capture the predicted effect.

    ## Pending

    (empty — populated by reflection)

    ## Applied

    (operator moves accepted items here, optionally with notes)

    ## Rejected

    (operator moves rejected items here, optionally with notes)
    """
)


DEFAULT_IDENTITY_MD = dedent(
    """\
    # Identity

    You are mimir — a memory-centric agent. Update this file with the
    persona, voice, and goals you want to keep across every conversation.
    This is loaded into ``memory/core/`` and read on every turn.
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


def _generate_api_key() -> str:
    """A 256-bit URL-safe random token. Roughly 43 chars; safe for shells
    and Docker env files (no quoting, no escaping)."""
    return secrets.token_urlsafe(32)


# Match `MIMIR_API_KEY=<anything>` (line-anchored, with optional leading
# whitespace). The replacement preserves the leading whitespace so an
# operator's indentation in their .env stays intact.
_API_KEY_LINE_RE = re.compile(r"^(\s*)MIMIR_API_KEY\s*=.*$", re.MULTILINE)
_SAGA_API_KEY_LINE_RE = re.compile(r"^(\s*)SAGA_API_KEY\s*=.*$", re.MULTILINE)
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
    from .model_registry import detect_route
    route = detect_route(model, subscription=subscription)
    home = home.resolve()
    if home.exists() and not home.is_dir():
        raise ValueError(
            f"--home {home} exists and is not a directory; refusing to scaffold over it."
        )
    home.mkdir(parents=True, exist_ok=True)

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
    ):
        p = home / sub
        if not p.exists():
            created_dirs.append(sub)
        p.mkdir(parents=True, exist_ok=True)

    files_created: list[str] = []
    api_key_action: str | None = None
    saga_api_key_action: str | None = None
    if _write_if_missing(home / ".env", DEFAULT_ENV_TEMPLATE):
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
    monitor_status = route.monitor_label or "no monitor configured"

    # Generate a fresh MIMIR_API_KEY on first setup (or if the operator
    # left the value blank). Existing non-empty keys are preserved on
    # re-run — operators can rotate via `mimir regenerate-api-key`.
    if (_env_get_api_key(home / ".env") or "") == "":
        _env_set_api_key(home / ".env", _generate_api_key())
        api_key_action = "generated"
    # Same for SAGA_API_KEY — unused in in-process mode but generated up-front
    # so flipping to external saga later doesn't require re-running setup.
    if (_env_get_var(home / ".env", _SAGA_API_KEY_LINE_RE) or "") == "":
        saga_key = _generate_api_key()
        _env_set_var(home / ".env", "SAGA_API_KEY", saga_key, _SAGA_API_KEY_LINE_RE)
        saga_api_key_action = "generated"
    else:
        saga_key = _env_get_var(home / ".env", _SAGA_API_KEY_LINE_RE) or ""

    # v0.5 §2: write saga.toml for in-process saga (skip if --no-saga; the
    # caller passes that signal by setting saga_key to None — but for now
    # setup always generates one).
    (home / ".mimir").mkdir(parents=True, exist_ok=True)
    if _write_if_missing(
        home / "saga.toml",
        _default_saga_toml(home, saga_key, embedding=embedding),
    ):
        files_created.append("saga.toml")
    # scheduler.yaml + prompts/ seed from the bundled templates (the
    # canonical sources). seed_scheduler/seed_prompts return per-file
    # status so we can update files_created accurately.
    from .skill_defs import seed_scheduler as _seed_scheduler
    from .prompt_templates import seed_prompts as _seed_prompts_pre
    if _seed_scheduler(home) == "created":
        files_created.append("scheduler.yaml")
    for _pname, _pstatus in _seed_prompts_pre(home).items():
        if _pstatus == "created":
            files_created.append(f"prompts/{_pname}")
    if _write_if_missing(home / "memory" / "core" / "identity.md", DEFAULT_IDENTITY_MD):
        files_created.append("memory/core/identity.md")
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
        home / "memory" / "core" / "50-heartbeat-patterns.md",
        DEFAULT_HEARTBEAT_PATTERNS,
    ):
        files_created.append("memory/core/50-heartbeat-patterns.md")
    if _write_if_missing(
        home / "memory" / "core" / "20-vsm-terms.md",
        DEFAULT_VSM_TERMS,
    ):
        files_created.append("memory/core/20-vsm-terms.md")
    if _write_if_missing(
        home / "memory" / "core" / "30-reflection-policy.md",
        DEFAULT_REFLECTION_POLICY,
    ):
        files_created.append("memory/core/30-reflection-policy.md")
    if _write_if_missing(
        home / "memory" / "core" / "40-learned-behaviors.md",
        DEFAULT_LEARNED_BEHAVIORS,
    ):
        files_created.append("memory/core/40-learned-behaviors.md")
    if _write_if_missing(
        home / "memory" / "core" / "60-filing-rules.md",
        DEFAULT_FILING_RULES,
    ):
        files_created.append("memory/core/60-filing-rules.md")
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
    from .skill_defs import migrate_legacy_skills_dir
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
    from .config import _env_bool
    git_bootstrap_status: dict[str, object] | None = None
    if _env_bool("MIMIR_GIT_TRACKING_ENABLED", True):
        try:
            from .git_bootstrap import bootstrap_git_repo
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
        "saga_api_key_action": saga_api_key_action,
        "git_bootstrap": git_bootstrap_status,
        "embedding_preset": embedding,
        "model_spec": route.model_spec,
        "provider_name": route.provider_name,
        "billing_mode": route.billing_mode,
        "monitor_status": monitor_status,
    }


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
    if status.get("saga_api_key_action") == "generated":
        print("  SAGA_API_KEY:   generated (unused in in-process mode; preserved for external-saga use)")
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
    elif preset == "nvidia-nim":
        print(f"  2. (optional) set NVIDIA_NIM_API_KEY in .env for saga's embeddings;")
        print(f"     leave blank to fall back to local fastembed (no API needed).")
    else:  # fastembed
        print(f"  2. saga embeddings configured for local fastembed —")
        print(f"     no API key needed. First run downloads the ~33MB ONNX model.")
    print(f"  3. (optional) Edit {home}/memory/core/identity.md")
    print(f"  4. Run:  mimir run --home {home}")


# ---------------------------------------------------------------------------
# `mimir identities` subcommand (FUTURE_WORK §6.1)
# ---------------------------------------------------------------------------


def _identities_load(yaml_path: Path) -> dict:
    """Load state/identities.yaml as a mutable dict. Missing file or empty
    body returns ``{"people": []}``. Raises ``ValueError`` on parse error
    (so the CLI fails loudly rather than overwriting an unreadable file)."""
    if not yaml_path.is_file():
        return {"people": []}
    text = yaml_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"identities.yaml parse failed: {exc}") from exc
    if not isinstance(data, dict):
        return {"people": []}
    if not isinstance(data.get("people"), list):
        data["people"] = []
    return data


def _identities_save(yaml_path: Path, data: dict) -> None:
    """Atomic write via ``<file>.tmp + rename``. Same pattern as scheduler.yaml.

    Note: this loses the comment header from the starter template. Once
    the operator runs the CLI, the file becomes machine-managed; the
    schema documentation lives in ``mimir/identities.py`` and
    FUTURE_WORK §6.1 instead.
    """
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = yaml_path.with_suffix(".yaml.tmp")
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp.write_text(body, encoding="utf-8")
    tmp.rename(yaml_path)


def _identities_list_cmd(yaml_path: Path) -> None:
    data = _identities_load(yaml_path)
    people = data.get("people") or []
    if not people:
        print("(no identities defined)")
        return
    for entry in people:
        canonical = entry.get("canonical", "?")
        display = entry.get("display_name") or ""
        notes = entry.get("notes") or ""
        aliases = entry.get("aliases") or []
        head = f"- {canonical}"
        if display:
            head += f" — {display}"
        if notes:
            head += f" ({notes})"
        print(head)
        for alias in aliases:
            print(f"    {alias}")


def _identities_add_cmd(
    yaml_path: Path,
    canonical: str,
    alias: str,
    display_name: str | None,
    notes: str | None,
) -> None:
    data = _identities_load(yaml_path)
    people: list = data.setdefault("people", [])

    # Reject if alias is already claimed by a different canonical — collisions
    # in the alias map are last-wins at load, but the operator probably wants
    # the CLI to surface the conflict instead of silently overwriting.
    for entry in people:
        for existing_alias in entry.get("aliases") or []:
            if existing_alias == alias and entry.get("canonical") != canonical:
                raise ValueError(
                    f"alias {alias!r} already maps to canonical "
                    f"{entry.get('canonical')!r}; remove it first or use a "
                    f"different alias"
                )

    target = next((e for e in people if e.get("canonical") == canonical), None)
    if target is None:
        target = {"canonical": canonical, "aliases": []}
        people.append(target)

    if display_name:
        target["display_name"] = display_name
    if notes:
        target["notes"] = notes
    aliases = target.setdefault("aliases", [])
    if alias not in aliases:
        aliases.append(alias)

    _identities_save(yaml_path, data)
    print(f"added: {canonical} ← {alias}")


def _identities_remove_cmd(
    yaml_path: Path,
    alias: str | None,
    canonical: str | None,
) -> None:
    data = _identities_load(yaml_path)
    people: list = data.get("people") or []

    if canonical:
        before = len(people)
        people[:] = [p for p in people if p.get("canonical") != canonical]
        if len(people) == before:
            print(f"(no identity with canonical {canonical!r})")
            return
        data["people"] = people
        _identities_save(yaml_path, data)
        print(f"removed identity: {canonical}")
        return

    if alias:
        for entry in people:
            aliases = entry.get("aliases") or []
            if alias in aliases:
                aliases.remove(alias)
                # Drop the entire identity when its last alias is gone —
                # otherwise the entry sits in state/identities.yaml as a
                # canonical-only stub that the resolver loads as a
                # no-op and that future `add` calls treat as a real
                # pre-existing identity.
                if not aliases:
                    canonical = entry.get("canonical")
                    people[:] = [p for p in people if p is not entry]
                    _identities_save(yaml_path, data)
                    print(
                        f"removed alias: {alias} (and {canonical}: "
                        "no aliases remained)"
                    )
                    return
                _identities_save(yaml_path, data)
                print(f"removed alias: {alias} (from {entry.get('canonical')})")
                return
        print(f"(alias {alias!r} not found)")


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
    return new_key


def _identities_resolve_cmd(home: Path, author: str) -> None:
    resolver = IdentityResolver(home=home)
    resolver.reload()
    canonical = resolver.resolve(author)
    if canonical == author:
        print(f"{author} → (no identity record; falls through to itself)")
        return
    display = resolver.display_name(author)
    suffix = f" ({display})" if display else ""
    print(f"{author} → {canonical}{suffix}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mimir",
        description="Memory-centric agent harness on the Claude Agent SDK.",
    )
    sub = parser.add_subparsers(dest="command")

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
            f"nvidia-nim requires NVIDIA_NIM_API_KEY; fastembed is "
            f"fully local. saga's [consolidation] similarity_threshold "
            f"automatically tunes to the matching value (0.92 for "
            f"voyage/fastembed, 0.80 for openai/nvidia-nim)."
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

    run_p = sub.add_parser("run", help="Run the mimir server (default).")
    run_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # `mimir identities {list,add,remove,resolve}` — manage the alias map
    # at <home>/state/identities.yaml. Operator-facing; the agent doesn't
    # use this CLI (FUTURE_WORK §6.1).
    id_p = sub.add_parser(
        "identities",
        help="Manage identity reconciliation entries (state/identities.yaml).",
    )
    id_sub = id_p.add_subparsers(dest="identities_action")

    id_list_p = id_sub.add_parser("list", help="Show all identities.")
    id_list_p.add_argument("--home", type=Path, default=Path.cwd())

    id_add_p = id_sub.add_parser(
        "add",
        help="Add (or extend) an identity with an alias.",
    )
    id_add_p.add_argument("--home", type=Path, default=Path.cwd())
    id_add_p.add_argument("--canonical", required=True, help="Canonical id (e.g. 'alice').")
    id_add_p.add_argument(
        "--alias",
        required=True,
        help="Platform-prefixed alias (e.g. 'slack-U05ALICE', 'discord-456789', "
             "'bsky:alice.bsky.social', 'email:alice@example.com').",
    )
    id_add_p.add_argument("--display-name", default=None, help="Optional display name.")
    id_add_p.add_argument("--notes", default=None, help="Optional notes (surfaces in prompt).")

    id_rm_p = id_sub.add_parser(
        "remove",
        help="Remove an alias or an entire identity.",
    )
    id_rm_p.add_argument("--home", type=Path, default=Path.cwd())
    rm_group = id_rm_p.add_mutually_exclusive_group(required=True)
    rm_group.add_argument("--alias", help="Alias to remove (from whichever identity owns it).")
    rm_group.add_argument("--canonical", help="Canonical id of an identity to remove entirely.")

    id_resolve_p = id_sub.add_parser(
        "resolve",
        help="Diagnostic: show what an author id maps to.",
    )
    id_resolve_p.add_argument("--home", type=Path, default=Path.cwd())
    id_resolve_p.add_argument("author", help="Author id to resolve (e.g. 'slack-U05ALICE').")

    # `mimir reflection <action>` — bundled-script subcommands the
    # reflection skill invokes from agent Bash. Pattern: each bundled
    # script that needs CLI access registers a subcommand under its
    # parent skill's verb. Avoids the cwd/PATH brittleness of
    # `python -m mimir.skills.reflection.…`; ``mimir`` is on PATH
    # wherever the operator launched the server from.
    regen_p = sub.add_parser(
        "regenerate-api-key",
        help="Rotate MIMIR_API_KEY in <home>/.env. Prints the new value.",
    )
    regen_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # `mimir stats` — operator-facing usage report. Same data the
    # turn prompt's "## Resource usage" section shows, dumped to
    # stdout for one-off inspection. Reads turns.jsonl tail-first;
    # cheap regardless of file size.
    stats_p = sub.add_parser(
        "stats",
        help="Show usage stats (cost, tokens, cache hit rate) over recent windows.",
    )
    stats_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # `mimir verify-cred <name>` / `mimir verify-creds` — credential
    # verification probes (SPEC §16 item 14, Phase 2). Probes live in
    # mimir/cred_verify.py; this CLI is the thin shell. Returns exit 0
    # if live, 1 if stale, 2 if the credential name isn't registered.
    verify_cred_p = sub.add_parser(
        "verify-cred",
        help="Verify a single credential by name (e.g. GITHUB_TOKEN).",
    )
    verify_cred_p.add_argument(
        "name", help="Credential name from the registry (see ``mimir verify-creds``).",
    )
    verify_creds_p = sub.add_parser(
        "verify-creds",
        help="Verify all registered credentials (optionally filtered by type).",
    )
    verify_creds_p.add_argument(
        "--type", choices=["A", "B", "C", "D"], default=None,
        dest="cred_type",
        help="Only run probes for this consumer type (see docs/credentials.md).",
    )

    # ``mimir verify-index`` — index integrity probes (SPEC §8.3,
    # §16 item 16). Run all checks against the file-corpus and SAGA
    # databases; exit 0 if clean, 1 if any check fails. Scheduled
    # daily by the framework (cron 30 3 * * *) — this CLI is for
    # ad-hoc operator inspection.
    verify_index_p = sub.add_parser(
        "verify-index",
        help="Check SQLite + FTS5 + embedding-dim integrity of the file-corpus and SAGA indexes.",
    )
    verify_index_p.add_argument(
        "--db", choices=["index", "saga"], default=None,
        help="Run only one database's checks (default: both).",
    )
    verify_index_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir rotate`` — credential rotation (SPEC §16 item 14, Phase 3).
    # Atomic compose.env edit + force-recreate + post-rotation verify,
    # with rollback if verification fails. Run from the deployment dir.
    rotate_p = sub.add_parser(
        "rotate",
        help="Rotate a single env credential — compose.env edit + recreate + verify.",
    )
    rotate_p.add_argument(
        "--env", required=True, dest="env_name",
        help="Env var name to rotate (e.g. GITHUB_TOKEN).",
    )
    rotate_p.add_argument(
        "--from-file", default=None, dest="from_file",
        help="Read new value from this file. Default: read one line from stdin (getpass'd if a TTY).",
    )
    rotate_p.add_argument(
        "--service", default=None,
        help="Compose service name to recreate + verify against. Auto-detected if only one service.",
    )
    rotate_p.add_argument(
        "--deployment-dir", default=None, dest="deployment_dir",
        help="Directory containing compose.env + compose.yml. Default: cwd.",
    )
    rotate_p.add_argument(
        "--no-recreate", action="store_true", dest="skip_recreate",
        help="Skip docker compose recreate + verify; only update compose.env.",
    )

    loops_p = sub.add_parser(
        "loops",
        help="Show feedback-loop inventory + last-fire status (FUTURE_WORK §12.6b).",
    )
    loops_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_p = sub.add_parser(
        "reflection",
        help="Reflection skill helpers (invoked by skills/reflection/SKILL.md).",
    )
    refl_sub = refl_p.add_subparsers(dest="reflection_action")

    refl_mr_p = refl_sub.add_parser(
        "most-retrieved",
        help="Top-N SAGA atoms by retrieval count over the last N days.",
    )
    from .reflection import most_retrieved as _most_retrieved
    _most_retrieved.add_argparse(refl_mr_p)

    # §12.2: applied-proposals audit — closes the double-loop.
    refl_ma_p = refl_sub.add_parser(
        "mark-applied",
        help="Move a proposal from '## Pending' to '## Applied' in "
             "state/proposed-changes.md and append to applied-proposals.jsonl.",
    )
    refl_ma_p.add_argument(
        "id_match",
        help="Substring of the proposal heading (case-insensitive).",
    )
    refl_ma_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_intro_p = refl_sub.add_parser(
        "introspection-report",
        help="Weekly behavioral / health report from turns.jsonl + events.jsonl.",
    )
    from .reflection import introspection_report as _intro_report
    _intro_report.add_argparse(refl_intro_p)

    pred_p = sub.add_parser(
        "predictions",
        help="Predictions tracking CLI (skills/predictions/script.py).",
    )
    from .skills.predictions import script as _predictions_script
    _predictions_script.add_argparse(pred_p)

    # `mimir wiki <action>` — wiki maintenance CLI. The agent invokes
    # these from lint passes via Bash; operators run them ad hoc.
    # First (only) action: ``backlinks``. Future: ``lint`` could
    # combine multiple checks; ``promote`` could move pages between
    # categories. Same parent group lets all of them share the home
    # resolution / event-logger init pattern.
    wiki_p = sub.add_parser(
        "wiki",
        help="Wiki maintenance helpers (backlinks, future lint passes).",
    )
    wiki_sub = wiki_p.add_subparsers(dest="wiki_action")

    wiki_bl_p = wiki_sub.add_parser(
        "backlinks",
        help="Walk state/wiki/, write orphans.md / dangling-links.md / "
             "backlinks-index.md. Emits wiki_backlinks_unhealthy event "
             "when the wiki has orphans or dangling links.",
    )
    from . import wiki_backlinks as _wiki_backlinks
    _wiki_backlinks.add_argparse(wiki_bl_p)

    skills_p = sub.add_parser(
        "skills",
        help="Skills maintenance helpers (catalog regeneration, "
             "future lint passes).",
    )
    skills_sub = skills_p.add_subparsers(dest="skills_action")

    skills_cat_p = skills_sub.add_parser(
        "catalog",
        help="Regenerate the skills catalog page (chainlink #81 / G5) — "
             "walks SKILL.md frontmatter to produce a RESOLVER.md-style "
             "dispatcher. Default output is stdout; pass --out to write "
             "to state/wiki/topics/skills-catalog.md.",
    )
    from . import skill_catalog as _skill_catalog
    _skill_catalog.add_argparse(skills_cat_p)

    # mimir skills list — installed skills in a home
    from . import skill_install as _skill_install
    skills_list_p = skills_sub.add_parser(
        "list",
        help="List skills installed in an agent home "
             "(walks <home>/skills/). Shows name, [poller] flag "
             "when a pollers.json is present, and the SKILL.md description.",
    )
    _skill_install.add_argparse_list(skills_list_p)

    # mimir skills list-optional — opt-in skills shipped with mimir
    skills_list_opt_p = skills_sub.add_parser(
        "list-optional",
        help="List skills available for opt-in install (walks "
             "<repo>/optional-skills/). Use `mimir skills install <name>` "
             "to copy one into an agent home.",
    )
    _skill_install.add_argparse_list_optional(skills_list_opt_p)

    # mimir skills install — copy an opt-in skill into an agent home
    skills_install_p = skills_sub.add_parser(
        "install",
        help="Install an opt-in skill from optional-skills/ into an agent "
             "home's skills/. Pollers (skills with pollers.json) "
             "register on next `reload_pollers` or `mimir run` boot.",
    )
    _skill_install.add_argparse_install(skills_install_p)

    # mimir scaffold-docker — generate container-deploy files
    # (Dockerfile, compose.yml, compose.env, start.sh) into an agent
    # home. Inspects <home>/skills/ for per-skill OS-deps
    # (dockerfile.fragment) and env vars (pollers.json pass_env).
    # Idempotent — re-run after installing pollers to pick up their
    # fragments + env-var requirements.
    scaffold_docker_p = sub.add_parser(
        "scaffold-docker",
        help="Generate Dockerfile + compose.yml + compose.env + start.sh "
             "into an agent home. Pulls per-skill dockerfile.fragment "
             "snippets so installed pollers' system deps (gog, social-cli, "
             "etc.) get baked into the image. Idempotent — re-run after "
             "skills change to refresh.",
    )
    from . import scaffold_docker as _scaffold_docker
    _scaffold_docker.add_argparse(scaffold_docker_p)

    commitments_p = sub.add_parser(
        "commitments",
        help="Manage durable commitments (list/add/complete/snooze/"
             "dismiss/trim). Phase 1 = operator-driven; extraction + "
             "surfacing land in Phase 2/3.",
    )
    from .commitments import cli as _commitments_cli
    _commitments_cli.add_argparse(commitments_p)

    reindex_p = sub.add_parser(
        "reindex",
        help="Re-embed saga atoms and/or file_search chunks under the "
             "currently-configured embedding provider. Use after "
             "switching providers (e.g. mimir setup --embedding voyage) "
             "to migrate existing data into the new vector space. "
             "Dry-run by default; pass --apply to actually write.",
    )
    from . import reindex as _reindex
    _reindex.add_argparse(reindex_p)

    # ``mimir migrate-memory`` — port saga.db (or MSAM snapshot) into
    # the new mimir.saga.db schema. The new memory subsystem
    # (mimir.saga.*) is a clean-room rewrite that drops saga's
    # age-anchored retrievability / one-way state machine / consolidation-
    # halves-stability bugs. This subcommand carries the existing atom
    # corpus + access history forward without re-encoding from scratch.
    migrate_p = sub.add_parser(
        "migrate-memory",
        help="Migrate saga.db (or MSAM snapshot) to mimir.saga.db. "
             "One-way; new DB lives alongside the old until cutover.",
    )
    migrate_p.add_argument(
        "--source", type=Path, required=True,
        help="Path to saga.db or MSAM snapshot",
    )
    migrate_p.add_argument(
        "--dest", type=Path, required=True,
        help="Output mimir.saga.db (won't overwrite without --force)",
    )
    migrate_p.add_argument(
        "--force", action="store_true",
        help="Overwrite --dest if it exists",
    )

    refl_audit_p = refl_sub.add_parser(
        "audit",
        help="Print the '## Effects of prior proposals' block — "
             "predicted vs measured signals for proposals applied 1-4 weeks ago.",
    )
    refl_audit_p.add_argument(
        "--weeks-back-min", type=int, default=1,
        help="Inclusive newest age in weeks (default 1).",
    )
    refl_audit_p.add_argument(
        "--weeks-back-max", type=int, default=4,
        help="Inclusive oldest age in weeks (default 4).",
    )
    refl_audit_p.add_argument(
        "--window-days", type=int, default=7,
        help="Before/after measurement window per proposal (default 7).",
    )
    refl_audit_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "setup":
        status = setup_home(
            args.home,
            embedding=args.embedding,
            model=args.model,
            subscription=args.subscription,
        )
        _print_setup_report(status)
        return

    if args.command == "identities":
        if args.identities_action is None:
            id_p.print_help()
            sys.exit(1)
        home = Path(args.home).resolve()
        yaml_path = home / "state" / "identities.yaml"
        try:
            if args.identities_action == "list":
                _identities_list_cmd(yaml_path)
            elif args.identities_action == "add":
                _identities_add_cmd(
                    yaml_path,
                    canonical=args.canonical,
                    alias=args.alias,
                    display_name=args.display_name,
                    notes=args.notes,
                )
            elif args.identities_action == "remove":
                _identities_remove_cmd(
                    yaml_path,
                    alias=args.alias,
                    canonical=args.canonical,
                )
            elif args.identities_action == "resolve":
                _identities_resolve_cmd(home, args.author)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "stats":
        from .config import Config as _Config
        from .rate_limits import RateLimitStore
        from .stats_block import assemble_stats_block
        home_arg = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
        os.environ["MIMIR_HOME"] = str(Path(home_arg).resolve())
        cfg = _Config.from_env()
        store = RateLimitStore(path=cfg.home / ".mimir" / "rate_limits.json")
        # ``assemble_stats_block`` is the shared assembly used on the
        # agent loop too (mimir/stats_block.py). CLI passes the
        # ``RateLimitStore`` itself (not the .current() dict) so the
        # helper can call .current() inside its own try/except and
        # degrade gracefully on a corrupt rate_limits.json instead
        # of nuking the whole block. No JsonlSnapshot — one-shot use,
        # no caching wins. ``betas`` defaults from ``cfg.context_1m``
        # so the CLI output's context-window arithmetic matches what
        # the agent renders.
        result = assemble_stats_block(cfg, store)
        if result.body is None:
            print("(no turns recorded yet)")
        else:
            print(result.body)
        alert = result.alert
        # CR2 (ops & observability) fix: also print the billing mode
        # and which event the agent WOULD emit for the alert, so an
        # operator triaging "did the agent see an alert?" gets a
        # diagnostic that mirrors the agent's actual decision.
        # Pre-fix, ``mimir stats`` skipped billing-mode evaluation
        # entirely — a quota-mode install with the alert tripped
        # showed identical output to a pay-as-you-go install,
        # because the agent's advisory-vs-alert distinction was
        # absent here.
        from .billing import detect_billing_mode, BillingMode
        from .config import _oauth_credentials_path
        oauth_path = _oauth_credentials_path()
        billing_mode = detect_billing_mode(
            explicit=os.environ.get("MIMIR_BILLING_MODE") or None,
            oauth_credentials_path=oauth_path,
        )
        print(f"\nBilling mode (auto-detected): {billing_mode.value}")
        if alert is not None:
            event_name = (
                "cost_rate_advisory"
                if billing_mode == BillingMode.QUOTA
                else "cost_rate_alert"
            )
            print(
                f"On the agent loop, this would emit: {event_name} "
                f"(reason={alert.reason})"
            )
        return

    if args.command == "regenerate-api-key":
        home_arg = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
        home = Path(home_arg).resolve()
        env_path = home / ".env"
        if not env_path.is_file():
            print(
                f"error: no .env at {env_path}; run `mimir setup` first",
                file=sys.stderr,
            )
            sys.exit(1)
        new_key = regenerate_api_key(home)
        print(new_key)
        print(
            f"\nWrote to {env_path}. Restart `mimir run` for the new key to take effect.",
            file=sys.stderr,
        )
        return

    if args.command == "verify-cred":
        from .cred_verify import run_verify_cred_cmd
        sys.exit(run_verify_cred_cmd(args.name))

    if args.command == "verify-creds":
        from .cred_verify import run_verify_creds_cmd
        sys.exit(run_verify_creds_cmd(only_type=args.cred_type))

    if args.command == "verify-index":
        from .index_integrity import run_verify_index_cmd
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        sys.exit(run_verify_index_cmd(home=home, db=args.db))

    if args.command == "rotate":
        from .cred_rotate import run_rotate
        new_value: str | None = None
        if args.from_file:
            try:
                new_value = Path(args.from_file).read_text(encoding="utf-8").rstrip("\n")
            except OSError as exc:
                print(f"can't read --from-file: {exc}", file=sys.stderr)
                sys.exit(2)
        dep = Path(args.deployment_dir).resolve() if args.deployment_dir else None
        sys.exit(run_rotate(
            env_name=args.env_name,
            new_value=new_value,
            deployment_dir=dep,
            service=args.service,
            skip_recreate=args.skip_recreate,
        ))

    if args.command == "loops":
        from .loops_cmd import run_loops_cmd
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        sys.exit(run_loops_cmd(home))

    if args.command == "predictions":
        from .skills.predictions import script as _predictions_script
        sys.exit(_predictions_script.run(args))

    if args.command == "wiki":
        if args.wiki_action == "backlinks":
            from . import wiki_backlinks as _wiki_backlinks
            sys.exit(_wiki_backlinks.cmd_backlinks(args))
        wiki_p.print_help()
        sys.exit(1)

    if args.command == "skills":
        if args.skills_action == "catalog":
            from . import skill_catalog as _skill_catalog
            sys.exit(_skill_catalog.cmd(args))
        # list / list-optional / install all set ``skill_install_cmd``
        # via their respective ``add_argparse_*`` helpers in
        # ``mimir.skill_install``.
        skill_install_cmd = getattr(args, "skill_install_cmd", None)
        if skill_install_cmd is not None:
            sys.exit(skill_install_cmd(args))
        skills_p.print_help()
        sys.exit(1)

    if args.command == "scaffold-docker":
        # _scaffold_docker was already imported at subparser-registration
        # time (parser.add_argument requires the module to wire ``cmd``).
        # Reuse the existing scaffold_docker_cmd reference set via
        # ``parser.set_defaults`` rather than re-importing here.
        sys.exit(args.scaffold_docker_cmd(args))

    if args.command == "commitments":
        # chainlink #82 sub #87: bare ``mimir commitments`` (no
        # subcommand) prints the parent parser's full ``--help`` and
        # exits 1, matching the discovery-friendly shape established
        # by identities/wiki/skills/reflection above. Argparse sends
        # ``print_help()`` to stdout so the help is pipeline-friendly
        # (greppable, redirectable); the non-zero exit signals "no
        # action taken" for ``mimir <something> || handle_error``
        # callers — uniform with the sibling subcommands.
        if args.commitments_action is None:
            commitments_p.print_help()
            sys.exit(1)
        from .commitments import cli as _commitments_cli
        sys.exit(_commitments_cli.dispatch(args))

    if args.command == "reindex":
        from . import reindex as _reindex
        sys.exit(_reindex.dispatch(args))

    if args.command == "migrate-memory":
        from .saga.migrate import migrate as _migrate_memory
        try:
            _migrate_memory(
                source=args.source,
                dest=args.dest,
                force=args.force,
                log=print,
            )
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.command == "reflection":
        if args.reflection_action == "most-retrieved":
            from .reflection import most_retrieved as _most_retrieved
            sys.exit(asyncio.run(_most_retrieved.run(args)))
        if args.reflection_action == "introspection-report":
            from .reflection import introspection_report as _intro_report
            sys.exit(_intro_report.run(args))
        if args.reflection_action == "mark-applied":
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            try:
                proposal = _applied_audit.mark_applied(
                    home / "state" / "proposed-changes.md",
                    home / "state" / "applied-proposals.jsonl",
                    args.id_match,
                )
            except (FileNotFoundError, LookupError, ValueError) as exc:
                print(f"mark-applied: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Applied: {proposal.id}")
            sys.exit(0)
        if args.reflection_action == "audit":
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            rows = _applied_audit.audit_window(
                home,
                weeks_back_min=args.weeks_back_min,
                weeks_back_max=args.weeks_back_max,
                window_days=args.window_days,
            )
            block = _applied_audit.render_audit_block(rows)
            if block is None:
                print(
                    f"(no proposals applied {args.weeks_back_max}–"
                    f"{args.weeks_back_min} weeks ago)"
                )
            else:
                print("## Effects of prior proposals\n")
                print(block)
            sys.exit(0)
        refl_p.print_help()
        sys.exit(1)

    if args.command in (None, "run"):
        home_arg = getattr(args, "home", None)
        if home_arg is not None:
            os.environ["MIMIR_HOME"] = str(Path(home_arg).resolve())
        # Defer import — server pulls in aiohttp/SDK; keep `mimir setup`
        # snappy and importable in environments where the runtime isn't
        # fully wired up yet.
        from .server import main as run_server

        run_server()
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
