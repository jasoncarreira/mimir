# Pre-Flight Protocol

Run this protocol **before** starting a climb. Pre-flight is a collaboration between the agent and the operator — the agent runs mechanical checks, the operator provides judgment on the harder questions.

A failed pre-flight saves tokens. Discovering mid-climb that the metric is noisy or the scope is leaking is expensive. Discovering it during pre-flight is free.

---

## Mechanical Checks

These can be verified programmatically or by inspection. The agent should present results to the operator.

### Law 1 — Is the metric orderable?

- [ ] Can two outputs be compared and ranked?
- [ ] Is the ordering stable across repeated evaluations? (Run the eval 3 times on the same input — do the results agree?)
- [ ] Is the granularity sufficient? (Can the metric distinguish the size of changes the climber will make?)

**Red flag:** If the same input produces different rankings on repeated evaluation, the metric fails Law 1. Fix the metric before climbing.

### Law 2 — Is the metric consistent?

- [ ] Does the same input score the same way twice?
- [ ] Is the measurement variance smaller than the expected improvement per iteration?
- [ ] For LLM judges: are questions binary (yes/no) rather than scalar (1-10)?

**Red flag:** If measurement variance exceeds expected improvement magnitude, the climb will be a random walk. Tighten the eval or increase the improvement target.

### Law 3 — Are changes reversible?

- [ ] Is there a tested revert mechanism? (Not just "we'll use git" — actually test the revert flow)
- [ ] Does reverting restore the exact previous state? (No residual artifacts)
- [ ] Can the climber revert automatically without operator intervention?

**Red flag:** If the revert mechanism is untested, the first failed iteration may corrupt state. Run a manual revert-and-verify before starting.

### Law 4 — Is scope separation enforced?

- [ ] Is the evaluation logic held in supervisor memory (preferred) or frozen files?
- [ ] Can the climber modify ONLY the designated workspace? (Nothing else is writable)
- [ ] Is there a verification step that detects if evaluation logic changed?

**Red flag:** If the climber has write access to evaluation files, Law 4 is violated by architecture. Restructure before climbing.

### Law 5 — Does the climber have enough context?

- [ ] Does program.md include domain knowledge relevant to the optimization target?
- [ ] Can the climber read failure cases and understand what went wrong?
- [ ] Are known failure modes documented so the climber doesn't re-discover them?

**Red flag:** If the climber's only context is "make the score go up," it will generate random changes. Provide specific context about what's failing and why.

---

## Judgment Checks

These require human judgment. The agent should present its assessment, but the operator makes the final call.

### S4 Maturity — Can gaming be detected?

- [ ] Is there a mechanism to detect when the metric is being gamed rather than genuinely improved?
- [ ] How quickly would gaming be detected? (Detection latency bounds the damage via Law 3)
- [ ] If S4 is weak (no established detection), are structural constraints compensating?

**Operator question:** "If the climber found a way to score well without actually improving, how long before we'd notice?"

**S4 maturity scaling:** When S4 is immature (new domain, new agent, no detection history), lean heavier on structural constraints — tighter scope, simpler metrics, shorter iteration cycles. As S4 develops (detection patterns emerge, false positives decrease), relax structural constraints and let the climber explore more freely. Structural constraints are training wheels, not permanent architecture.

### S5 Clarity — Do we know why this hill matters?

- [ ] Can the operator articulate what success means beyond the metric?
- [ ] Is there a clear connection between metric improvement and actual value?
- [ ] Will the operator know when to stop climbing and pick a new hill?

**Operator question:** "If the score hits 1.0, are we done? What does 'done' actually mean for this problem?"

S5 is what makes metric pivots different from metric drift. Without clear purpose holding the long-term direction, fast detection (S4) plus fast pivoting (Law 3) just means fast wandering. The operator must know why this hill matters — not just that it exists.

### Budget — Is iteration cost acceptable?

- [ ] What does one iteration cost? (tokens, time, compute)
- [ ] How many iterations can the budget support?
- [ ] At what point do diminishing returns kick in?
- [ ] For slow-feedback domains (e.g., predictions): what is the minimum viable cycle length?

**Operator question:** "If this takes 50 iterations at $X each, is that worth the expected improvement?"

---

## Pre-Flight Summary

After running all checks, present a summary to the operator:

```
PRE-FLIGHT SUMMARY
==================
Climb: {climb_id}
Target: {what we're optimizing}

MECHANICAL CHECKS:
  Law 1 (orderable):     PASS / FAIL — {details}
  Law 2 (consistent):    PASS / FAIL — {details}
  Law 3 (reversible):    PASS / FAIL — {details}
  Law 4 (scope sep):     PASS / FAIL — {details}
  Law 5 (informed):      PASS / FAIL — {details}

JUDGMENT CHECKS:
  S4 maturity:           {assessment} — {operator decision needed}
  S5 clarity:            {assessment} — {operator decision needed}
  Budget:                {cost estimate} — {operator approval needed}

RECOMMENDATION: Ready to climb / Fix {issues} first
```

The operator approves or rejects. Pre-flight is a gate, not a suggestion.
