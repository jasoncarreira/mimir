---
name: issues-audit
description: Monthly triage of the memory/issues/ operational-gotcha layer. Run when a turn fires with trigger=scheduled_tick on channel scheduler:issues-audit (the operator wires the cron entry; this prompt drives the audit). For each gotcha file decide keep / retire / raise-as-chainlink-bug / escalate-to-operator. Autonomous for clear-cut cases (retire already-RESOLVED files, file chainlink bugs); propose-to-operator for judgment calls. Batches across runs to stay within the tool-call budget. Sibling of commitments-review. Tracked as chainlink #164.
---

# Issues audit

A monthly triage of `memory/issues/` — the operational-gotcha layer.
Each file is a fingerprint-shaped runbook for a failure mode you might
hit again (see `memory/issues/README.md`). Over time entries go stale:
bugs get fixed, infra changes, PRs land. Without periodic review the
layer bloats with junk and the real gotchas get buried. This pass keeps
it tight — and converts the latent-bug backlog hiding inside it into
tracked chainlink issues.

Sibling pass: the weekly `commitments-review`. Keep them separate — one
turn doing both would blow the per-turn tool-call budget.

## Step 1 — Scope this run (budget-aware)

List the layer, oldest-first:

```bash
ls -tr "$MIMIR_HOME"/memory/issues/*.md
```

There are usually dozens of files; one turn can't read + verify all of
them inside the tool-call budget. Work a **batch of ~25–30 files per
run**, oldest-by-mtime first (recently-edited gotchas are usually still
active and can wait). Track where you stopped in
`state/audit/issues-audit-cursor.md` — a single line with the last
filename audited and the date. Next month, resume *after* that file, wrapping to
the top when you reach the end. A full cycle therefore spans as many
monthly runs as the file count needs — that's expected, not a failure.

Skip `README.md` and any index file.

## Step 2 — Triage each file in the batch

Read each gotcha (its first-line `<!-- desc: -->` plus body give you the
failure mode and its runbook). Pick exactly one verb:

- **keep** — the failure mode can still occur. No action. This is the
  default when you're unsure and the entry is recent or specific.
- **retire** — superseded, resolved, or no longer fingerprint-shaped.
  - *Autonomous* only when the entry already says RESOLVED / FIXED in
    its desc or body, **or** names a specific PR/commit you can confirm
    landed (`git -C "$MIMIR_WORKSPACE" log --oneline --grep=...` or by
    reading the now-fixed code). Then delete the file.
  - *Escalate* (do **not** delete) when it's ambiguous — maybe-still-
    relevant, or you can't confirm the fix. Add it to the operator
    digest as a retire candidate.
- **raise-as-chainlink-bug** — the gotcha describes a real, current,
  code-level bug worth tracking and fixing (not just working around).
  File it (you run inside the container, so `chainlink` is on PATH —
  call it directly, not via docker):
  ```bash
  chainlink issue create "<short title>" \
      -d "<failure mode + the source memory/issues/<file> + repro/fix sketch>" \
      -p <low|medium>
  ```
  Then append a line to the gotcha file noting it's tracked as chainlink
  #N, so next month's audit doesn't re-file it. Filing is autonomous —
  it's tracking, not a code change.
- **escalate-to-operator** — infra debt or a recurring pain point the
  operator should consciously decide about (not a discrete code bug).
  Digest only; no file change.

## Step 3 — Apply + rebuild the index

After any autonomous **retire** deletions, rebuild the master index so
the every-turn `memory/INDEX.md` desc list reflects the removals — call
the `rebuild_index` tool (`scope="memory"`).

## Step 4 — Log + escalate

1. Log the run to chainlink #164 for an audit trail:
   ```bash
   chainlink issue comment --kind result 164 "Audit <date>: batch of N (oldest X .. Y). kept K / retired R (<files>) / raised B bugs (#…) / escalated E. Cursor now at <last-file>."
   ```
2. If there were any **escalate-to-operator** items (retire candidates,
   infra decisions) or newly-filed bugs, send a short digest to the
   operator alert channel with `send_message` (channel
   `$MIMIR_OPERATOR_ALERT_CHANNEL`). If that var is unset, log a one-line
   warning to stderr and skip the send.

## Step 5 — End the turn

Scheduled-tick turns end silently (no user-visible reply), like
heartbeats. The chainlink #164 comment and the operator digest are the
only outward signals.

## Self-reminders

- Budget discipline: ~25–30 files per run, write the cursor, resume next
  month. A partial pass is correct.
- **retire** is the verb with teeth — it deletes. Autonomous only for
  RESOLVED / landed-PR cases; everything ambiguous escalates.
- **raise-as-chainlink-bug** is the highest-leverage output: it turns a
  stored workaround into a tracked fix.
- A clean batch (all keep) is fine — log the one-line result and end.
