<!-- desc: live, in-place-edited "Working" activity panel in Slack/Discord — a passive metadata-only per-turn step tracker driven by the TurnEventBus -->

# Activity panel (live tool-call panel for Slack / Discord)

**Status:** shipped (2026-06-30). Opt-in; off by default; no behavior change for
channels that don't enable it. Epic [#718]; slices #719/#720/#721 (chainlinks),
polish #724, trigger-gating #725.

The activity panel is a passive, live-updating message posted to a chat channel
that shows what the agent is doing during a turn — a "Working" panel that
accumulates the turn's steps (`Thought`, `Ran <tool>`, an in-flight `Working`
row) and **edits itself in place** as the turn progresses, then finalizes and
(after a real reply) removes itself. It is the chat-side analog of the web
dashboard's live timeline (`frontend/src/LiveActivityPanel.tsx`, the "Field
log"). It is entirely separate from the agent's explicit `send_message` output —
reactions and replies stay explicit; the panel is automatic activity display.

## How it works

- **Source — no turn-loop changes.** The panel consumes the in-process
  `TurnEventBus` (`mimir/turn_event_bus.py`), which the model loop already
  publishes to: `turn` (start/end), `tool_call`, `tool_result`, and `reasoning`
  spans, channel-keyed, non-blocking and drop-allowed. The panel just
  subscribes; it never blocks or slows the turn.
- **Subscriber.** `mimir/bridges/_activity_panel.py` `ActivityPanel` runs as its
  own asyncio task (started in `build_app`/`server.py` only when the allow-list
  is non-empty), consumes its own bounded queue, and swallows per-event errors.
- **Lifecycle (all via one edited message).** On `turn`/`start` it posts the
  panel via `bridge.send` (capturing `SendResult.message_id`), threaded on the
  triggering message where supported (Slack `thread_ts`); on span transitions it
  edits in place via `bridge.edit_message`; on `turn`/`end` it collapses to a
  compact `Done` / `✓ N steps` summary and, if an explicit outbound-message
  signal says a real reply was posted, **deletes** the panel after a short grace
  (~2s). It reconciles to the final state on turn end, so a dropped bus event
  never leaves a stuck spinner.
- **Debounce.** Edits are coalesced to ~1/sec per channel (Slack `chat.update`
  is tier-3, per-channel posting ~1/sec); it never edits per streamed chunk.
- **Bridge capability.** Requires an in-place edit on the bridge:
  `Bridge.edit_message(channel_id, message_id, MessageUpdate)` — Slack
  `chat.update` (text + Block Kit blocks), Discord `message.edit` (text +
  embed). `MessageUpdate` is a typed payload (`text` / `blocks` / `embed`); each
  bridge ignores the fields it can't render. Finalize-delete uses
  `Bridge.delete_message`. Bridges without these inherit a safe no-op default
  (bench/web), so the panel simply never appears there.

## Configuration

Both are prefix allow-lists / maps over channel ids; both default off/coarse.

- `MIMIR_ACTIVITY_PANEL_CHANNELS` — comma-separated channel-id prefixes the
  panel is enabled for (e.g. `discord-,slack-`). Empty = off. The panel only
  posts for **user-facing work turns**; internal/system triggers never surface
  one (see gating below), regardless of channel.
- `MIMIR_ACTIVITY_PANEL_DETAIL` is retained as a compatibility setting but no
  longer changes panel content. All panels are metadata-only.

## Trigger gating (chainlink #725)

The panel only posts for turns whose trigger is user-facing work. `handle_event`
gates the `turn`/`start` branch via `trigger_enabled(trigger)` **before** the
panel model is created (a skipped turn produces no panel and no model, so there
are no downstream edits/finalize). The trigger is already carried on the
turn-start bus event (`_trigger_metadata`).

- Included (posts): `user_message`, `poller`, `scheduled_tick`,
  `shell_job_complete`.
- Excluded (no panel): `saga_session_end` (idle-session synthesis / session
  boundary), `upgrade`, `claude_code_spawn`, `react_received`, `reflect`, and
  any unknown/unclassified trigger (skipped by default — a new framework trigger
  cannot surface a panel until it's explicitly classified).

## Step labels

Derived from the span type + `tool_name` by `_step_label` (coarse-only vocabulary
— never raw args):

- `reasoning` → `Thought`.
- `tool_call` (in-flight) → `Calling <tool>` / `Working <tool>`; the completed
  tool renders a single `Ran <tool>` result row (the redundant call row is
  dropped, and `tool_name` is carried on `tool_result/end` so it's the real
  tool name, not a generic fallback).

## Information-flow boundary

The panel payload contains only harness-authored status vocabulary, sanitized
tool names, step counts, and folded-input counts. It does not retain or render
reasoning text, tool arguments or results, inbound message bodies, attachment
paths, or author identity. This makes it harness display state rather than turn
content egress; `activity_panel_post` and `activity_panel_edit` therefore use the
dedicated `harness_display` sink category. Model-invoked `send_message` and
`react` remain `same_channel` sinks and keep their IFC checks.

Each non-chunk span transition requests an edit, but edits are coalesced to at
most about one per second per channel. The observed edit-heavy ratio is expected:
one panel is posted per turn and then rewritten around tool/reasoning transitions.
The debounce already coalesces bursts; further coalescing would make live status
less responsive and is not needed for authorization correctness.

## Not this

- Not a `send_message` replacement or a change to reply/reaction behavior.
- Not spoiler/thread-based collapse — the "collapse" is simply the next in-place
  edit; the in-flight detail is transient by design.
- Not persisted per-step detail — completed rows show a label only (a
  field-journal-style persist-under-each-step view was considered and declined).
