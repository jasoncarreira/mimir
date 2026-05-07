---
name: async-tasks
description: Turn "block until condition X is met" into an agent wake-up. Use when you need to wait for a one-shot event without burning context — a webhook arriving, a CI pipeline finishing, a file appearing, a long shell command completing — AND you want this conversation to resume with full context when the event fires. Distinct from pollers (recurring) and `long-running-jobs` (sync bash detach without conversation-resume). The mimir-native analog is a subagent that blocks on the condition and notifies on completion.
---

# Async Task Patterns

The deeper move behind any "block until X" primitive is:

> **Anything that blocks until a condition is met can be turned into an agent wake-up.**

If you can write a one-liner that blocks until *X*, you've just built "wake me up when
X happens" into the conversation flow.

## What "wake me up" looks like in mimir today

Mimir doesn't yet have a single-call `shell(..., async_mode=True)` that auto-fires a
completion event. The cleanest implementation is a heartbeat-backlog item — see
`state/heartbeat-backlog.md` "async shell jobs." Until that lands, the **subagent**
mechanism (the `Task` tool) is the closest fit:

* Issue a Task with a prompt like *"block until X happens, then return the result."*
* The subagent runs as long as it needs to (subject to the subagent's own time
  budget). It can use `bash` to run blocking commands, poll endpoints, watch files.
* When the subagent finishes, mimir emits a `subagent_notification` event. The next
  turn (or an immediate turn if the channel was idle) picks up the result via the
  inbox; the parent conversation resumes with full context.

Functionally equivalent to async-shell + completion-event, just with an LLM-mediated
wrapper. More expensive in tokens than a raw shell call would be — the subagent's
prompt costs — but already available.

The other two adjacent primitives:

* **Pollers** (`pollers.json` next to skills) — recurring scripts that run on the
  scheduler tick and emit events. Right tool when the same condition will be checked
  forever; wrong tool when you only need *this* conversation to resume on *this*
  specific event.
* **`long-running-jobs`** — bash + nohup + tee + PID tracking. Sync detach for
  fire-and-forget background work; the agent does NOT auto-resume when it finishes.
  Use when you don't care about conversation-resume.

## The killer pattern: block on the human

The shell command runs an OS-native modal dialog. The dialog blocks until the user
clicks. The agent's turn ends, the conversation suspends, and the moment the human gets
back to their machine and clicks OK, the agent resumes — with the original task still on
its mind.

**macOS** wrapped in a Task subagent:

```
Task: "Run this command in bash, then return the result:
osascript -e 'display dialog \"OAuth session expired. Re-auth, then click OK.\"
              with title \"Mimir paused\" buttons {\"OK\"} default button \"OK\"'"
```

The agent kicks off the Task, the turn completes, the operator's screen shows a dialog.
When they click OK the shell exits, the subagent returns, the `subagent_notification`
fires, the next turn picks up exactly where it left off.

`display dialog` can also collect input — `default answer ""` returns the typed text in
the result — turning it into a synchronous "ask the human a question and wait" primitive.

**Linux**: `zenity --question`, `zenity --entry`, `kdialog --inputbox` — same shape, all
block until clicked.

**Windows**: a one-liner PowerShell `[System.Windows.MessageBox]::Show(...)` blocks the
same way.

For the *notify* side (a non-blocking ping that the human should look at the dialog),
send a Discord/Slack message first; then the subagent kicks off the dialog. The full
pattern is **notify + block**: send a notification so they know, then block on the
dialog itself.

## What else blocks?

Once you have "spawn a blocking command in a subagent, wake me when it returns," all of
these become agent wake-up triggers:

**Wait for a file to appear** — useful for "the human will drop a CSV in this folder":

```bash
inotifywait -e create -q --format %f /watch/dir   # Linux
fswatch -1 /watch/dir                             # macOS
```

**Wait for a webhook** — block on a TCP listener for a one-shot HTTP POST:

```bash
nc -l 9999 | head -c 4096
```

Pair with an ngrok / cloudflared URL and you've got "wake me when this URL is hit."
Useful for OAuth callbacks, GitHub webhook deliveries, "click here when done" links.

**Wait for CI to finish**:

```bash
gh pr checks 123 --watch
```

GitHub's CLI blocks until the checks resolve. The subagent's return value gives you the
result.

**Wait for a deploy / cloud build** — most cloud CLIs have `--wait` or foreground modes:

```bash
gcloud builds submit --config=cloudbuild.yaml
kubectl rollout status deploy/api --timeout=10m
```

**Wait for a condition to become true** — the universal `until` loop:

```bash
until curl -sf https://api.example.com/health; do sleep 10; done
```

This is where async overlaps `pollers` — and the distinction matters. Use a poller when
the condition will be polled *forever* and many wake-ups are expected. Use async-block
when this is a *one-shot* "wait for this specific thing, then resume *this* task." The
async pattern preserves your in-flight reasoning; pollers create fresh wake-ups.

**Wait for a long-running job you didn't start** — `wait $PID`, or:

```bash
tail --pid=12345 -f /dev/null
```

Blocks until process 12345 exits.

**Wait for a Slack reaction / GitHub PR approval / email reply** — usually easier as a
poller, but if you need *this* conversation to resume (with all its context) when the
event fires, write an `until` loop that polls and exits on match, run it inside a
subagent, and let the subagent return be your callback.

## The pattern: notify + block

The compound move:

1. Agent realizes it can't proceed (session expired, decision needed, input required)
2. Agent fires a notification via Discord/Slack so the operator's phone/desktop pings
3. Agent spawns a Task subagent whose body is the blocking command
4. Agent's turn completes — token cost for the parent stops, the harness shows "waiting"
5. Operator handles the thing, clicks OK / drops the file / hits the webhook
6. Blocking command exits, subagent returns, notification fires
7. Parent conversation resumes with full context, finishes the task

This is dramatically better than two alternatives:

* **Synchronous block** — burns tokens for hours, hits timeouts, the conversation is
  pinned waiting for one human action.
* **"Just stop and the human can re-prompt"** — context is gone. The human has to
  re-explain the task. Often the agent has done substantial setup work that's wasted.

The async-block pattern preserves the work-in-progress across the human-time gap.

## Composing with pollers

Pollers and async-tasks are duals:

* **Pollers** run forever on a schedule, emit events, wake *any* turn / fresh
  conversation.
* **Async tasks** are one-shot, block on one specific event, wake *this* parent
  conversation / preserve this thread.

When to convert one to the other:

* If a poller keeps firing for the same in-flight task, it's wasting your prelude work
  — consider an async-block-until instead.
* If an async-block has been waiting for hours and the operator might send several
  similar requests over time, you probably want a poller.

## When to reach for what

| Need | Tool |
|---|---|
| Quick command, need result now | sync `Bash` |
| Long command, don't care when it finishes | `long-running-jobs` (nohup + tee) |
| Long command, resume *this conversation* when done | **subagent wrapping the command (this file)** |
| Wait for repeating events forever | `pollers` |
| Run on a fixed cadence (cron) | `add_schedule` / `scheduler.yaml` |
| Wait for *one* event, preserving conversation | **subagent with a blocking command** |
| Parallel LLM work that doesn't need this conversation | subagent with a regular prompt |

The async-block-via-subagent pattern is the only mimir primitive that preserves the
current reasoning across an indeterminate wait.

## Pre-spawn discipline

The subagent return is a fresh wake-up. The parent turn that picks it up has rebuilt
context — the in-memory variables you held while issuing the Task are gone; what's on
disk survives. **Anything future-you needs to know on the wake-up side has to be
written before you spawn.**

Concrete:

* Log a chainlink interest item or update a state file with the task's *intent*: why
  you spawned it, what success means, what failure looks like.
* Tag durable artifacts with a unique key. "Posted draft #abc123 to channel" — if the
  wake-up fires twice (rare but possible) and you see #abc123 already exists, skip
  rather than duplicate.
* Treat the wake-up turn as a fresh-context turn. Don't assume in-flight reasoning
  carried over.

## Anti-patterns

* **Don't use async-block for things that take seconds.** Just await synchronously.
  The wake-up cycle has overhead.
* **Don't use it for recurring conditions.** That's what pollers are for. Async-block
  is one-shot.
* **Don't block on something that might never happen** without a timeout. Wrap the
  blocking command in `timeout 1h ...` if you can't trust the human / event to arrive.
* **Don't forget the pre-spawn write.** The wake-up turn is fresh context — leave the
  intent on disk before spawning so future-you knows what was happening.
* **Don't chain async-blocks naively.** Each wake-up is a separate turn with its own
  context-rebuild cost. If you need multiple gates, consider whether they can be one
  shell pipeline (`gate1 && gate2 && gate3`) inside a single subagent.

## Composing with other skills

* **`pollers`** — the dual primitive. Pollers are recurring; async-tasks are one-shot.
* **`long-running-jobs`** — the sync-detach primitive for "run in background and
  forget." Use this when you don't need the conversation to resume on completion.
* **`circuit-breaker`** — if the same async-block keeps timing out, that's a loop. The
  breaker should trip and the structural fix usually involves either a poller (the
  condition is recurring) or a different gate.
* **`fallback-chains`** — when the terminal "loud failure" rung is `display dialog
  "Everything failed, please intervene"`, that's an async-block waiting for the human.
