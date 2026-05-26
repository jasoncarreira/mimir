---
name: fallback-chains
description: Layer alternative channels / scrapers / data sources with explicit fall-through, where each rung covers a different failure mode. Use when interacting with the world (sending a notification, fetching from a third party, detecting a state change, authenticating) and the single canonical path could fail in known ways. Distinct from retrying — retries handle transient failures; fallback chains handle modal ones (channel down, API gone, cookie expired). Three layers tops; the terminal rung is loud.
---

<!-- desc: Layer alternative channels/scrapers/data sources with explicit fall-through; each rung covers a different failure mode (not retries). -->

# Fallback Chains

The instinct when interacting with the world is to pick *one* channel, *one* scraper,
*one* data source — and pray it works. Real systems have outages, expired tokens,
rate limits, and quiet failures. The pattern is to **layer alternatives with explicit
fall-through**, where each layer covers a different failure mode.

This is not the same as retrying the same thing harder. Retrying handles *transient*
failures; fallback chains handle *modal* failures (the channel is down, the API is
gone, the cookie expired). Each rung of the chain assumes the previous rung is broken.

## Anatomy of a fallback chain

Three things make a chain useful rather than ceremonial:

1. **Each layer covers a *different* failure mode.** Slack → Slack-via-different-bot
   is not a chain; it's superstition. Discord → email → ntfy.sh push → state-file
   marker is a chain — each rung survives the previous rung's typical failures.
2. **Detection has to be cheap.** If checking whether a layer succeeded costs as much
   as the layer itself, the chain is more expensive than the failures it prevents.
3. **The terminal rung is loud.** When the whole chain falls through, *somebody* needs
   to know. Often: write a state file marker AND page the human via the most reliable
   channel you have left.

## Concrete chains worth stealing

**Reaching the operator** (when the agent needs human attention):

```
1. Discord/Slack DM (fastest if they're at a device)
2. Email (survives chat-platform outage, archived)
3. ntfy.sh push to phone (works anywhere with internet)
4. write state/operator-needs-attention.md (terminal — picked up next session)
```

The agent should not iterate this in real-time on every send. Pick the right *initial*
rung based on urgency and time-of-day; fall through only on detected failure.

**Scraping data behind a login**:

```
1. Official API if one exists
2. Undocumented internal API the page calls (snarf via network capture)
3. HTML parse from rendered page
4. Screenshot + vision read of the rendered page
```

Each rung is more brittle but works on more sites. The trick is *not* to start at #4 —
start at #1, fall through only when the upstream fails.

**Knowing about a state change** (composes with `world-scanning`):

```
1. Webhook (push from the source — if they support it)
2. Long-poll subscription
3. Cron-poll the API every N minutes
4. Cron-poll the public HTML page
```

Push is cheaper and faster but needs the source's cooperation. Pull always works.

**Authentication**:

```
1. Cached token in keyring / session
2. Refresh token flow
3. Re-login flow (probably needs the human; chain into "reach the operator")
```

**Saga retrieval** (mimir-specific):

```
1. memory_query with the relevant terms (typed retrieval)
2. Direct file Read of memory/core/ + state/wiki/ if the answer should be canonical
3. Glob-by-keyword across memory/ as a last resort
4. Ask the operator (loud — the canonical info isn't where it should be)
```

When step 2 wins repeatedly, the answer should be in saga as an observation; file a
chainlink interest item.

## When NOT to add fallbacks

Fallback chains protect *transport*, not *truth*. If the failure mode is "the data is
wrong," more channels won't help — you'll just have wrong data faster.

* Don't fall back across data sources that disagree. Pick one source of truth and live
  with its outages, or surface the disagreement to the human.
* Don't fall back to a worse version of the same thing because the canonical version
  is "slow." Slow is not a failure mode.
* Don't add fallbacks "just in case" without a concrete failure mode in mind. Each
  rung costs maintenance — when one breaks silently, the chain is now lying.

## The verification problem

A fallback chain is only as useful as your ability to detect that a rung *actually*
delivered. Many channels fail silently — a Slack message to a bot that's been removed
returns 200 OK and goes to /dev/null.

Mitigations:

* **Confirm-on-receive when possible.** Webhooks acknowledged by the receiver. Discord
  message you re-fetch after sending to confirm presence. Saga store calls return
  atom_ids you can later query for.
* **Heartbeats.** If the channel has been quiet for too long, treat it as down and
  fall through. (See `world-scanning` "Inversions.")
* **Periodic end-to-end tests.** A scheduled job that exercises the whole chain and
  alerts if any rung silently broke.

## Don't overengineer

A two-rung chain is a *huge* upgrade from a one-rung chain. The marginal value drops
fast: fourth and fifth rungs are usually dead weight that nobody maintains.

The right shape for most agent code:

```
primary  →  one well-chosen backup  →  loud failure
```

Three layers tops, unless the cost of total failure is genuinely catastrophic.

## Composing with other skills

* **`world-scanning`** — pull/push fallback for change detection. The pollers menu is
  full of "primary push, fall back to pull" shapes.
* **`circuit-breaker`** — falling through every rung repeatedly is itself a pattern
  to break on. If the chain has fired three times in an hour, stop and investigate
  the structural problem rather than draining the chain again.
* **`introspection`** — `logs/events.jsonl` is where you debug *which* rung failed
  and why. A fallback chain that fires often is a signal worth investigating.
* **`try-harder`** — when the chain keeps falling through, the structural fix is
  often *to remove a dead rung*, not to add another one.
