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
