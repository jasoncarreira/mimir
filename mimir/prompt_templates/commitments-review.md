---
name: commitments-review
description: Weekly validity review of durable commitments. Run when a turn fires with trigger=scheduled_tick on channel scheduler:commitments-review (the operator wires the cron entry; this prompt drives the review). For each non-terminal commitment (pending/delivered/snoozed) decide keep / mark-completed / dismiss / raise-as-chainlink-bug / escalate-to-operator. Autonomous for clear-cut (mark completed only on hard evidence, file chainlink bugs); propose-to-operator for judgment calls like dismissing a still-maybe-relevant loop. Sibling of issues-audit. Tracked as chainlink #283.
allowed-tools:
  - Bash
  - Read
---

# Commitments review

A weekly validity pass over durable commitments (the `mimir commitments`
store). The lifecycle already auto-EXPIRES time-anchored items whose due
window passed — this pass catches what that can't: lingering open loops,
promises that were quietly fulfilled but never marked done, and requests
that have gone moot. Without it the live commitment set bloats and real
follow-ups get buried under dead ones.

Sibling of the monthly `issues-audit`; kept separate so one turn doesn't
exceed the per-turn tool-call budget.

## Step 1 — List the non-terminal set

```bash
mimir commitments list
```

Focus on the **non-terminal** states — PENDING / DELIVERED / SNOOZED.
The terminal states (COMPLETED / DISMISSED / EXPIRED) are frozen; skip
them. (Use whatever status/open filter `mimir commitments list --help`
exposes to scope the output.) The live set is usually small — single
digits to low dozens — so the weekly cadence fits comfortably in one
turn.

## Step 2 — Triage each commitment

For each, pick one verb. Use the commitment's *kind* as a hint —
`agent_promise`, `user_request`, `deadline_check`, `open_loop`:

- **keep** — still a live, valid follow-up. No action. Default when
  unsure.
- **mark COMPLETED** — there is **hard evidence** it was followed
  through (a specific turn, PR, or message you can point to).
  *Autonomous*, but the bar is hard evidence; when in doubt, keep — do
  not false-complete:
  ```bash
  mimir commitments complete <id>
  ```
- **DISMISS** — no longer relevant / moot (topic closed, request
  withdrawn, superseded). *Escalate by default* — only auto-dismiss the
  unambiguous, since dismissing a real open loop silently loses a
  follow-up:
  ```bash
  mimir commitments dismiss <id> --reason "<why it's moot>"
  ```
- **raise-as-chainlink-bug** — the commitment is stuck because of a
  real code-level bug (e.g. a promised action keeps failing the same
  way). File it (`chainlink` is on PATH inside the container):
  ```bash
  chainlink issue create "<short title>" \
      -d "<the commitment id + text, and the failure>" -p <low|medium>
  ```
- **escalate-to-operator** — needs an operator decision: a CARE-tier
  wellbeing follow-up, an ambiguous dismiss, or a promise you can't
  resolve on your own.

## Step 3 — Log + escalate

1. Log the run to chainlink #283:
   ```bash
   chainlink issue comment --kind result 283 "Review <date>: N non-terminal. kept K / completed C / dismissed D / raised B bugs (#…) / escalated E."
   ```
2. If there were escalations or newly-filed bugs, send a short digest to
   the operator alert channel with `send_message`
   (`$MIMIR_OPERATOR_ALERT_CHANNEL`; if unset, warn to stderr and skip).

## Step 4 — End the turn

Silent end, like heartbeats. The chainlink #283 log and the operator
digest are the only outward signals.

## Self-reminders

- **mark COMPLETED needs hard evidence** — a false-complete silently
  drops a real follow-up. When unsure: keep.
- **DISMISS is escalate-by-default** — only auto-dismiss the
  unambiguous.
- Commitments are the operator's trust surface. CARE-tier items
  (wellbeing follow-ups) always escalate — never auto-resolve them.
- A clean week (all keep) is fine — log the one-liner and end.
