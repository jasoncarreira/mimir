# Spec: Discord typing indicator that holds for the full turn

<!-- desc: typing indicator drops after 10s; should hold until the bot sends its reply -->

**Status:** filed 2026-05-05. Not started. Single focused PR.

## Problem

Discord shows the "Mimir is typing…" indicator for ~10 seconds on a
single typing trigger. Mimir's bridge currently fires that trigger
once, fire-and-forget, when the inbound message lands. For turns that
take longer than 10s — most of them, since adaptive thinking + tool
calls regularly run 30-90s — the indicator drops while the bot is
still working. The user sees nothing happening, can't tell whether
the bot received the message, and pings again unnecessarily.

## Current shape (the broken bit)

`mimir/bridges/discord.py:335-359`:

```python
async def send_typing_indicator(self, channel_id: str) -> None:
    """Show the Discord typing dots in the channel. Discord's API
    renders the indicator for ~10s on a single POST. Failures
    swallowed; typing is a UX nicety, not load-bearing."""
    ...
    if hasattr(channel, "typing"):
        async with channel.typing():
            pass                  # ← exits immediately
```

The docstring even names the limitation: "for longer turns it'll just
expire naturally." That's what we're fixing.

discord.py's `channel.typing()` is an async context manager that:

- On `__aenter__`: POSTs once to the typing API (Discord renders ~10s)
  AND schedules a background task that re-POSTs every 9s while the
  context is live.
- On `__aexit__`: cancels the background task. No refresh, no further
  posts. Discord drops the indicator ~10s after the last post.

By exiting the context immediately with `pass`, mimir cancels the
auto-refresh right away. The first POST already went out, so the
indicator shows for ~10s, then dies.

## How lettabot does it (reference)

`open-strix/open_strix/discord.py:617-674`:

```python
@asynccontextmanager
async def _typing_indicator(self, event):
    ...
    async with channel.typing():
        try:
            yield                 # ← typing held for the full body
        finally:
            self.log_event("typing_indicator_stop", ...)
```

The `_typing_indicator` IS a context manager. It's wrapped around the
whole turn (`async with self._typing_indicator(event): await run_turn(...)`).
discord.py's auto-refresh keeps the indicator alive for as long as the
caller stays inside the `async with`.

## Why we can't copy-paste lettabot's shape

Open-strix processes channel events synchronously: bridge handler
calls `run_turn` directly, so a single context-manager wrap covers
the whole turn cleanly.

Mimir's bridge handler **enqueues** the event onto a per-channel
mailbox; a separate worker picks it up later and runs the turn. The
bridge handler returns immediately. There's no single async scope
that spans both "inbound landed" and "outbound delivered" — they're
separate functions in different tasks.

So we need a different shape: bridge-side state that holds the typing
context *across* the enqueue/dequeue gap and is cancelled when the
reply actually goes out.

## Proposed implementation

A per-channel asyncio task that holds the typing context until
cancelled. Cancellation happens when the bridge's `send()` is called
(real reply going out) or after a hard 5-minute upper bound (errored
turn that never sends).

### Sketch

```python
class DiscordBridge:
    def __init__(self, ...):
        ...
        # Per-channel long-lived "hold typing open" tasks. Created on
        # inbound, cancelled on outbound (or hard-capped at 5min).
        self._typing_tasks: dict[str, asyncio.Task] = {}

    async def send_typing_indicator(self, channel_id: str) -> None:
        """Hold the Discord typing indicator open until ``send()`` is
        called for this channel (or ~5min, whichever first). Replaces
        the previous fire-and-forget single-POST shape, which dropped
        the indicator after Discord's 10-second TTL even when the bot
        was still working."""
        # Cancel any prior typing task on this channel — most recent
        # inbound wins.
        prior = self._typing_tasks.pop(channel_id, None)
        if prior is not None and not prior.done():
            prior.cancel()

        if self._client is None or self._client.is_closed():
            return
        cid_int = _channel_id_to_int(channel_id)
        if cid_int is None:
            return

        async def _hold() -> None:
            try:
                channel = self._client.get_channel(cid_int)
                if channel is None:
                    channel = await self._client.fetch_channel(cid_int)
                typing_ctx = getattr(channel, "typing", None)
                if typing_ctx is None:
                    return
                async with typing_ctx():
                    # discord.py auto-refreshes every ~9s while the
                    # context is live; we just need to stay inside it.
                    await asyncio.sleep(300)  # 5min hard cap
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Typing is a UX nicety, not load-bearing.
                pass

        self._typing_tasks[channel_id] = asyncio.create_task(_hold())

    async def send(self, channel_id: str, ...) -> SendResult:
        # Cancel the typing-hold task if one is live for this channel —
        # the actual reply is going out so the dots should stop.
        prior = self._typing_tasks.pop(channel_id, None)
        if prior is not None and not prior.done():
            prior.cancel()
        # ... existing send logic unchanged
```

### Files touched

- `mimir/bridges/discord.py` — the change above
- `tests/test_discord_bridge.py` (or wherever DiscordBridge tests
  live) — add coverage for the new behavior

### Lifecycle

```
inbound message  → send_typing_indicator(channel)
                     → cancels any prior task on that channel
                     → spawns _hold() task that enters channel.typing()
                       (POSTs immediately + auto-refreshes every 9s)

worker processes turn (anywhere from 1s to several minutes)

agent emits reply → bridge.send(channel, text)
                     → cancels _hold() task
                     → exits channel.typing() context
                     → next 9s tick is skipped → Discord drops indicator
                       within ~10s

OR: turn errors / never sends → _hold() hits the 300s sleep timeout
                     → exits the context naturally → indicator drops
```

### Edge cases the implementation must handle

- **Repeated inbound from same channel (user keeps typing):** the
  `pop + cancel` at the top of `send_typing_indicator` ensures the
  newest call wins. Old task is cancelled cleanly.
- **Bridge disconnects mid-task:** the inner `try/except Exception`
  swallows it; typing is best-effort.
- **`channel.typing` missing** (mock channel, older discord.py):
  early return, no task spawned. Matches existing behavior.
- **`send()` called for a channel that never had a typing task**
  (scheduled tick replies, programmatic sends): `pop()` returns
  `None`, no-op. Safe.
- **Multiple sends per turn** (paginated long replies): the first
  send cancels typing; subsequent sends find no task and no-op. Right
  semantics — once the first chunk is out, the indicator should stop.
- **`asyncio.CancelledError` re-raised inside `_hold`:** standard
  practice — don't swallow CancelledError.

### Test plan

- `test_typing_holds_until_send` — simulate 30s turn, assert
  `channel.typing` was kept entered (mock receives multiple
  refresh calls, not just one), and that exit happens on `send()`.
- `test_typing_replaced_on_repeat_inbound` — two `send_typing_indicator`
  calls in a row; first task cancelled, second still alive.
- `test_typing_capped_at_five_minutes` — without a `send()` call,
  the task exits via the 5-minute timeout.
- `test_send_without_prior_typing_task_is_safe` — `send()` works when
  no typing task exists for that channel (scheduled tick path).
- `test_typing_on_unknown_channel_is_noop` — `_channel_id_to_int`
  returns None / `get_channel` returns None; no task spawned.

### Out of scope

- Slack typing indicator (Slack's API has a similar `typing`
  primitive but its semantics differ — separate PR if/when wanted).
- Web chat typing indicator (the web bridge would need its own UI
  channel for "is typing" state — separate PR).
- Surfacing typing-state events into `events.jsonl`. Lettabot logs
  `typing_indicator_start` / `typing_indicator_stop` events; we don't
  today and probably don't need to — turn lifecycle events
  (`turn_started` / `turn_finished`) already bracket the same window
  with more useful metadata.
