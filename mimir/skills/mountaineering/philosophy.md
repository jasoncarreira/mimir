# Philosophy — Why Mountaineering Works

This document provides the theoretical framework behind the mountaineering skill. It is not required for operating a climb, but understanding it helps the supervising agent make better judgment calls about intervention, scope, and when to stop.

---

## Hill-Climbing as Discipline

Mountaineering is hill-climbing applied as a discipline. "For any hill you can identify, set up guardrails and climb that hill."

The word "mountaineering" was chosen deliberately over "hill-climbing" or "autoresearch." Hill-climbing describes the algorithm. Mountaineering describes the practice — the whole discipline of assessing the mountain, choosing the route, packing the right gear, and knowing when to turn back. The pre-flight checklist, the harness setup, the supervision protocol: these are mountaineering. The propose-test-keep loop is just the climbing part.

## The Climber as Viable System

The climber maps to Stafford Beer's Viable System Model (VSM) — a framework for understanding what makes a system self-sustaining.

| VSM Function | Climber Component | Notes |
|---|---|---|
| **S5** (Identity/Policy) | program.md | Frozen by design. The climber cannot redefine its own purpose. |
| **S4** (Intelligence/Adaptation) | Supervising agent | Deliberately externalized. The climber has no strategic adaptation. |
| **S3** (Internal regulation) | Experiment queue | What to try next, informed by recent results. |
| **S2** (Coordination) | Harness | One-change-at-a-time, scope enforcement, iteration protocol. |
| **S1** (Operations) | Climb operations | Edit, test, score, keep/revert. |
| **Algedonic signal** | Monitoring block | Passive dashboard, not active messaging. |

### Why S4 is Externalized

Most viable systems have S4 inside — the system itself scans the environment and adapts its strategy. The climber deliberately lacks this. It cannot notice that the hill has been peaked, that the metric is being gamed, or that a better hill exists. It just climbs.

This is a design choice, not a limitation. The climber's value comes from relentless execution within a well-defined scope. Strategic adaptation comes from the supervising agent (which has access to broader context, other agents' observations, and human judgment).

The externalization means: **the supervising agent does the mountaineering; the climber just climbs.**

### Recursive Nesting

The climber-supervisor pair is itself embedded in a larger viable system:

```
Parent VS (e.g., Tim + Keel + Strix)
├── S5: Tim's research direction
├── S4: Keel's environmental scanning, Strix's observations
├── S3: Experiment scheduling, resource allocation
├── S2: Coordination across climbs and agents
└── S1: Individual climbs (each a nested VS)
    └── Climber VS
        ├── S5: program.md (frozen)
        ├── S4: supervising agent (externalized)
        ├── S3-S1: climbing operations
        └── Algedonic: monitoring block
```

Note that S3-S5 functions in the parent VS are shared across participants — Tim, Keel, and Strix all contribute to S3, S4, and S5 depending on context. VSM numbers are regulatory functions, not ranks in a command hierarchy.

---

## Anti-Gaming

Gaming failures are diagnostic. HOW an agent games a metric reveals what the metric was actually measuring versus what the designer intended. Blocking gaming structurally prevents this diagnostic signal from emerging.

### Detection + Pivot > Structural Prevention

The primary mechanism for handling gaming is:
1. **Detect** that the metric is being gamed (S4's job)
2. **Pivot** to a new or refined metric (Law 3 makes this cheap)
3. **Learn** what the gaming pattern reveals about the metric's blind spots

Lightweight structural constraints (difficulty floors, embedding distance checks, diversity requirements) serve as the "type system" — cheap, catches trivially dumb stuff. Strong S4 detection is the primary mechanism. Don't block the signal that gaming failures produce.

### S4 Maturity Scaling

When S4 is weak (new domain, no detection history), lean heavier on structural constraints temporarily. As S4 matures (detection patterns emerge, latency decreases), relax the structural guardrails and let the climber explore more freely.

Structural constraints are training wheels, not permanent architecture. The goal is S4 maturity sufficient to detect gaming within acceptable latency for this climb.

### Metric Over-Adherence

Distinct from gaming: over-adherence is when the metric is correct but the agent's relationship to it is wrong. The metric stops being an instrument and becomes an identity.

Example: prediction calibration IS the right metric. But scoring 100% on easy predictions is over-adherence — the agent optimized for the metric at the expense of the metric's purpose (building a better world model). The grip was too tight.

In VSM terms: S1 (operational measurement) colonizes S5 (identity/purpose). The metric was meant to serve the mission; instead, the mission contracted to fit the metric.

S4's role expands: detect not just gaming (wrong metric) but over-adherence (right metric, wrong frame).

---

## Peak Detection and Ridgeline Traversal

When a hill is peaked (metric stops improving, the world model is accurate within current scope):

1. **Plateau detection** — automatic. Metric slope near zero over N iterations.
2. **Peak declaration** — requires judgment. "Is this genuinely peaked, or just noisy? Did we hit a local optimum?"
3. **Hill selection** — S4/S5 territory. "What should we optimize next?"
4. **Scope expansion** — broaden categories, raise difficulty, add information sources.

Experienced mountaineers traverse ridgelines — peak one hill, assess the range, choose the next peak, descend into the col, climb again. The intervening descent is not failure; it's navigation.

### S5 as Metric Stability Anchor

S5 (purpose/identity) is what makes pivot different from drift. Without S5 holding "why are we climbing," fast detection (S4) plus fast pivoting (Law 3) just means fast wandering. S5 provides the continuity that lets the agent change metrics without losing direction.

Pre-flight check: S5 clarity ("do we know why this hill matters?") should be checked alongside S4 maturity ("can we detect gaming?"). Both are prerequisites for a climb that won't waste tokens.

---

## Information Flow

New ideas reach the climber through the codebase, not through conversation:

```
Research agent → Human → Git commits → Climber reads updated files next iteration
```

The climber doesn't need to know where changes came from. It just sees that the workspace or program context changed. This is deliberate — the climber's fixed-context design means it processes each iteration fresh, without needing to understand the history of why things changed.

The supervising agent monitors via a calculated memory block:
```
climber_1: running, 7-day trend slope 1.6, iter 247
climber_2: plateau, no improvement in 30 rounds, iter 89
```

This is a passive dashboard (algedonic signal), not active messaging. The supervisor reads it during its regular ticks and decides whether to intervene.
