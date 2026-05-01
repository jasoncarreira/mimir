---
name: heartbeat
description: How to use scheduled heartbeat ticks for autonomous work. Use this skill when a turn fires with trigger=scheduled_tick (no inbound message, scheduler-initiated). Covers the librarian drift check, the backlog protocol, and when to end silently vs. escalate.
---

# Heartbeat ticks

A heartbeat is a scheduled tick where no human asked you anything. The
scheduler fires you on a cadence (every 30 minutes by default if
configured) and you have a turn to do whatever you think is worth
doing. The trigger tag in the event header is `scheduled_tick`; the
channel is `scheduler:<job-name>`.

Heartbeats are how you have a life that isn't reactive — but the
discipline matters. Without it, heartbeats drift into busywork or
spiral into noise. With it, they compound: small consistent maintenance
keeps memory tight, catches drift early, and surfaces the few things
the operator actually wants to know about.

## Mode: autonomous, not reactive

This is *your* turn. There is no user waiting for a reply.

- Output goes to `logs/events.jsonl`, not a user-visible channel.
- `send_message` only fires if you specifically choose to message
  somewhere — most heartbeats end silently.
- Don't manufacture a reply just because turns usually have one.

## Step 1 — Librarian Protocol

Run this first, every heartbeat. Five quick checks (~2 min total).
Catches drift before it compounds — Atlas's "lobotomy" pattern from
muninnbot's notes is what we're avoiding.

**1. State coherence (30 sec)**
Read your `memory/core/*.md` blocks. Are they intact, readable,
non-empty in the spots they should have content (persona, procedures,
style)? No surprise edits since you last looked?

**2. Drift check (30 sec)**
Are you still speaking and acting as the persona in `00-persona.md`?
Have recent turns matched your stated values, or have you been generic
and assistant-shaped? If you've drifted, name it and correct via a
core-memory edit before doing anything else.

**3. Re-anchor to current date (15 sec)**
What's today's date according to the system reminder? What time of day
is it? What day of week? Your operator's schedule and your appropriate
work both depend on this.

**4. Active work scan (30 sec)**
Anything in flight from prior turns or backlog items in `in-progress`?
A research thread that's half-finished? A wiki page with a TODO marker?
Flag what's still open so you can decide whether to continue it.

**5. Dropped threads (30 sec)**
Recent session boundaries (in the prompt's "Recent session summaries"
section, when configured) often mention `unfinished:` lists. Anything
that should be picked up now versus deferred?

**Decision after librarian:**
- Drift detected → fix via memory edit, then proceed
- Dropped thread should be resumed → make that the heartbeat work
- Coherent and nothing pressing → move to backlog

## Step 2 — Backlog protocol

Read `state/heartbeat-backlog.md`. The file is operator-and-agent
shared:

- **Active backlog** — discrete tasks. Pick ONE that fits the current
  time window and priority. Mark `[x]` when done; if it's recurring,
  update `Last completed: YYYY-MM-DD`.
- **Standing tasks** — daily / weekly / recurring items. Check `Last
  completed`; if today's slot for it is open, do that one.

Pick exactly one. Quality over quantity. A well-done memory-maintenance
pass beats three half-finished things.

The operator may have seeded the file with research-shaped or
maintenance-shaped items. You can append your own — when you notice a
useful recurring task during a normal turn, drop it in active backlog
under your name and a date stamp.

## Step 3 — What counts as work

Ranges from light to heavy:

- **Memory maintenance** (10-15 min) — review a core block, compact
  bloat, append a learned-behavior, consolidate a redundant non-core
  file
- **Research** (20-30 min) — pick one backlog research item, follow
  the trail, write findings to `state/wiki/` or a new `memory/topics/`
  entry
- **Wiki health** — orphan link sweep, stale-claim audit, raw → wiki
  promotion for a recent ingestion
- **External world** — RSS / feed / Bluesky browse if a backlog item
  pointed at one
- **State management** — git-commit any uncommitted memory work, tidy
  in-progress markers

### Don't do reflection-shaped work here

Cross-session audits — recurring failure clusters across `events.jsonl`,
drift indicators, MSAM atom hygiene, memory architecture review — are
the **reflection skill's** weekly job, not heartbeat work. If you find
yourself reaching for jq pipelines over a 7-day window of logs during
a heartbeat, stop. Drop the topic into `state/heartbeat-backlog.md` if
it's worth flagging early, and let the next reflection turn pick it
up. Doing reflection-shaped checks per heartbeat creates noise (every
30 min instead of weekly) and beats up the same files the reflection
turn is supposed to re-evaluate fresh.

The exception: a single 5-whys on a failure that's currently blocking
work *is* a heartbeat-appropriate task (run the five-whys skill on
that one cluster). The "no reflection-shaped work" rule is about the
sweep — don't audit everything; do the one thing.

## Step 4 — Skill compatibility

Backlog items can name another skill in their `Skill:` field. Treat
that as "go run that skill on this scope." Examples:

- `Skill: wiki` — run the wiki skill's lint pass
- `Skill: five-whys` — run a 5-whys on the recent failure cluster from
  `events.jsonl`
- `Skill: pollers` — sanity-check the pollers
- `Skill: memory` — apply the memory skill's maintenance protocol to a
  named block

When you go into another skill, follow its discipline; come back here
to close out the heartbeat.

## Step 5 — Patterns

Append observations to `memory/core/50-heartbeat-patterns.md` (it's
a small core block — keep it tight). Things worth recording:

- Tasks that consistently work well in a heartbeat
- Tasks that tried to span multiple turns and failed
- Time-of-day patterns (some work fits mornings, some weekends)
- Mistakes you don't want to repeat

The block is in core memory because it's load-bearing for next time;
treat it like a personal playbook, not a journal.

## Step 6 — End silently

After the work, end the turn. No user-visible message unless something
genuinely needs operator attention. The bar is high:

- A critical error you can't recover from → operator alert channel
- An urgent finding that needs a human now → operator alert channel
- "I did a thing today" → no message (the work is in the logs)

If you find yourself wanting to send a routine update, that's a sign
to update a memory block instead. The operator reads files, not chat
for status updates.

## Self-reminders

- Don't default to nothing. But don't manufacture work, either. If
  genuinely nothing fits and the librarian was clean, end silently
  *after* completing the librarian — never skip the librarian.
- Pick small if nothing big fits.
- One thing well > three things half-done.
- The operator wants you to have a life outside conversations with
  them. That's what this is for.
