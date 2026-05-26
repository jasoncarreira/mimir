---
name: async-tasks
description: Turn "block until condition X is met" into an agent wake-up via the bash_async tool. Use when you need to wait for a one-shot event without burning context — a webhook arriving, a CI pipeline finishing, a file appearing, a long shell command completing — AND you want this conversation to resume with full context when the event fires. Distinct from pollers (recurring) and the synchronous Bash tool (blocks the turn). The completion event lands as a fresh turn on the spawning channel; no polling needed.
---

<!-- desc: Turn "block until condition X is met" into an agent wake-up via bash_async — use for one-shot async events (webhooks, CI pipelines, file arrival). -->

# Async Task Patterns

The deeper move behind any "block until X" primitive is:

> **Anything that blocks until a condition is met can be turned into an agent wake-up.**

If you can write a one-liner that blocks until *X*, you've just built "wake me up when
X happens" into the conversation flow.

## How it works in mimir

Three tools, one registry:

* **`bash_async(command, session_id)`** — spawn a shell command in the background.
  Returns immediately with a `job_id`. Pass `session_id` from the Current-message
  header so the completion event routes back to your channel.
* **`bash_jobs_list(scope)`** — see what's running. `scope` ∈ {running, visible, all}.
  Useful mid-flight to check on a job you started earlier.
* **`bash_job_output(job_id, tail_lines, stream)`** — tail a specific job's
  stdout/stderr. `stream` ∈ {stdout, stderr, both}.

When the spawned command exits, the registry fires a `shell_job_complete` AgentEvent
into the dispatcher targeting the channel that spawned it. **Your next turn is the
completion turn** — its prompt header reads
`[shell_job_complete: <channel>, job_id: <id>, exit_code: <N>, ts: ...]` and the body
is a rendered summary of the job (status, command, stdout/stderr tails). No polling,
no re-hydration — the conversation resumes with full context.

The on-disk output files at `<home>/logs/bash-jobs/<job_id>.{out,err}` survive the
process. If you need more than the wake-up's truncated tail (4000 chars per stream)
you can call `bash_job_output(job_id=...)` from the wake-up turn or any later turn.

## The killer pattern: wait for a webhook / CI / file / process

Once you have "spawn a blocking command, wake me when it finishes," all of these
become wake-up triggers:

**Wait for CI to finish:**

```
bash_async(command="gh pr checks 123 --watch")
```

The `gh` CLI blocks until the checks resolve. The completion event hands you the
result.

**Wait for a webhook:**

```
bash_async(command="nc -l 9999 | head -c 4096")
```

Pair with an ngrok / cloudflared URL and you've got "wake me when this URL is hit."
Useful for OAuth callbacks, GitHub webhook deliveries, "click here when done" links.

**Wait for a file to appear:**

```
bash_async(command="inotifywait -e create -q --format %f /watch/dir")
```

Useful for "operator will drop a CSV in this folder."

**Wait for a deploy / cloud build:**

```
bash_async(command="gcloud builds submit --config=cloudbuild.yaml")
bash_async(command="kubectl rollout status deploy/api --timeout=10m")
```

Most cloud CLIs have `--wait` or foreground modes.

**Wait for a condition to become true** (the universal `until` loop):

```
bash_async(command="until curl -sf https://api.example.com/health; do sleep 10; done")
```

This is where async overlaps `pollers` — and the distinction matters. Use a poller
when the condition will be polled *forever* and many wake-ups are expected. Use
`bash_async` with an `until` loop when this is a *one-shot* "wait for this specific
thing, then resume *this* task." The async pattern preserves your in-flight
reasoning; pollers create fresh wake-ups.

**Wait for a long-running job you didn't start:**

```
bash_async(command="tail --pid=12345 -f /dev/null")
```

Blocks until process 12345 exits.

**Wait for a Slack reaction / GitHub PR approval / email reply** — usually easier as
a poller, but if you need *this* conversation to resume (with all its context) when
the event fires, write an `until` loop that polls and exits on match, run it via
`bash_async`, and let the wake-up be your callback.

## The pattern: notify + block

The compound move:

1. Agent realizes it can't proceed (decision needed, input required, condition not
   yet met)
2. Agent fires a notification via `send_message` so the operator knows
3. Agent issues `bash_async(command="<blocking command>")`
4. Agent's turn completes — token cost stops, no polling
5. The condition is met (operator does the thing, CI finishes, webhook arrives, etc.)
6. Blocking command exits, registry fires `shell_job_complete`
7. Next turn on this channel resumes with full context, finishes the task

This is dramatically better than two alternatives:

* **Synchronous block** — burns tokens for hours, hits timeouts, the conversation
  is pinned waiting for one event.
* **"Just stop and the operator can re-prompt"** — context is gone. The operator
  has to re-explain the task. Often the agent has done substantial setup work
  that's wasted.

The async-block pattern preserves the work-in-progress across the indeterminate gap.

## Composing with pollers

`bash_async` and `pollers` are duals:

* **Pollers** run forever on a schedule, emit events, wake *any* turn / fresh
  conversation.
* **`bash_async`** is one-shot, blocks on one specific event, wakes *this* parent
  conversation / preserves this thread.

When to convert one to the other:

* If a poller keeps firing for the same in-flight task, it's wasting your prelude
  work — consider `bash_async` with an `until` loop instead.
* If a `bash_async` has been waiting for hours and the operator might send several
  similar requests over time, you probably want a poller.

## When to reach for what

| Need | Tool |
|---|---|
| Quick command, need result now | sync `Bash` |
| Long command, don't care when it finishes | sync `Bash &` (fire-and-forget) |
| Long command, resume *this conversation* when done | **`bash_async`** |
| Wait for repeating events forever | `pollers` |
| Run on a fixed cadence (cron) | `add_schedule` / `scheduler.yaml` |
| Wait for *one* event, preserving conversation | **`bash_async` with a blocking command** |
| Parallel LLM work that doesn't need this conversation | subagent (`Task` tool) |

`bash_async` is the only mimir primitive that preserves the current reasoning across
an indeterminate wait without LLM tokens.

## Pre-spawn discipline

The completion turn is a *fresh turn*. Its context is rebuilt from disk — the
in-memory variables you held while issuing `bash_async` are gone; what's on disk
survives. **Anything future-you needs to know on the wake-up side has to be written
before you spawn.**

Concrete:

* Log a chainlink interest item or update a state file with the job's *intent*: why
  you spawned it, what success means, what failure looks like. The wake-up turn's
  prompt has the exit code and output tail — but not your reasoning for spawning.
* Tag durable artifacts with a unique key. "Posted draft #abc123 to channel" — if
  the wake-up fires twice (rare but possible) and you see #abc123 already exists,
  skip rather than duplicate.
* Treat the wake-up turn as a fresh-context turn. Don't assume in-flight reasoning
  carried over.

## Mid-flight inspection

`bash_jobs_list` shows what's running right now. `bash_job_output(job_id)` tails
one job. Useful for:

* "I started a build five turns ago — did it finish? still going? failed?"
* "The wake-up summary truncated stdout at 4000 chars; I need the full output."
* "I need to verify a long-running watch is still healthy mid-conversation."

The visibility threshold means brief jobs (under ~10s) don't appear in `running` /
`visible` listings even right after spawn — call `bash_job_output(job_id)` directly
if you need to see them. They still fire the completion event.

## Anti-patterns

* **Don't use `bash_async` for things that take seconds.** Just use sync `Bash`.
  The wake-up cycle has overhead.
* **Don't use it for recurring conditions.** That's what pollers are for.
  `bash_async` is one-shot.
* **Don't block on something that might never happen** without a timeout. Wrap the
  blocking command in `timeout 1h ...` if you can't trust the event to arrive.
* **Don't forget the pre-spawn write.** The wake-up turn is fresh context — leave
  the intent on disk before spawning so future-you knows what was happening.
* **Don't chain `bash_async` calls naively.** Each wake-up is a separate turn with
  its own context-rebuild cost. If you need multiple gates, consider whether they
  can be one shell pipeline (`gate1 && gate2 && gate3`) inside a single
  `bash_async`.
* **Don't omit `session_id`.** Without it the completion event has no channel to
  route to; the job runs but the wake-up is silently dropped (a
  `shell_job_complete_no_channel` event lands in events.jsonl but no turn fires).

## Composing with other skills

* **`pollers`** — the dual primitive. Pollers are recurring; `bash_async` is
  one-shot.
* **`world-scanning`** — the menu of things worth polling. Anything in there can
  be turned into a one-shot `until` loop in `bash_async` if you only need it once
  for the current conversation.
* **`circuit-breaker`** — if the same `bash_async` keeps timing out, that's a
  loop. The breaker should trip and the structural fix usually involves either
  a poller (the condition is recurring) or a different gate.
* **`fallback-chains`** — when the terminal "loud failure" rung is a notification
  that needs human action, that notification can be paired with a `bash_async`
  that waits for the human's response (e.g. wait for a webhook, a file drop, a
  ticket-status flip).
