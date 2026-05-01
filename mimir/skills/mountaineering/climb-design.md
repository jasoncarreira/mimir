# Phase 0: Climb Design

The hardest part of mountaineering is figuring out WHAT to climb. Pre-flight checks whether a climb is viable. Climb design figures out what the climb IS. Without this phase, you show up at pre-flight with a vague objective and no metric — and pre-flight correctly tells you you're not ready, but doesn't help you GET ready.

This phase turns "I want to improve X" into a fully specified climb that can pass pre-flight.

---

## Step 1: Name the Objective in Plain Language

State what you want to improve in one sentence, without jargon. No metrics yet, no implementation details. Just the thing.

Examples:
- "Make the bot's responses more helpful when users are frustrated"
- "Reduce the time it takes to process incoming events"
- "Improve prediction accuracy for agent world models"
- "Make the newsletter intro copy more compelling"

**Why this matters:** If you can't say what you're optimizing in plain language, you don't understand it well enough to build a metric for it. The plain-language statement is the S5 anchor — it's what you'll come back to when evaluating whether a metric is measuring the right thing or just measuring something convenient.

---

## Step 2: Instrument — What Data Do You Already Have?

Before designing a metric, take inventory of what's already observable. You can't measure what you can't see.

**Questions to answer:**
- What outputs does the system currently produce?
- What logs, scores, or feedback already exist?
- What could you observe with zero additional infrastructure?
- What would require new instrumentation?

**Common discovery:** You often have more signal than you think. Existing logs, user reactions, downstream effects, error rates — these are all potential metric inputs hiding in plain sight.

**Common trap:** Designing an elaborate metric when a simple proxy is sitting right there. Check what's available before building something new.

---

## Step 3: Candidate Metrics

List 2-3 ways to measure progress toward the objective. For each candidate, evaluate against Laws 1 and 2:

### For each candidate metric, ask:

**Law 1 check — Is it orderable?**
- Can you compare two outputs and say which is better?
- Is the ordering stable? (Same comparison, same answer)
- Is the granularity sufficient to detect the size of changes the climber will make?

**Law 2 check — Is it consistent?**
- Does the same input score the same way twice?
- Is measurement variance smaller than expected improvement per iteration?
- For LLM judges: are questions binary (yes/no) rather than scalar (1-10)?

### Fast feedback vs. full metric

Not all metrics arrive at the same speed. Distinguish:

- **Proxy metrics (immediate):** Binary signals available right now. Did the output parse? Did the test pass? Is the response under N tokens? These give you signal on iteration 1. Use them for the first 10 steps to confirm the climb is moving at all.
- **Full metrics (accumulated):** Require a window of data to be meaningful. Calibration scores, trend analysis, statistical significance. These are what you optimize toward, but they need 10+ data points before they stabilize.

**The 10-step checkpoint:** After 10 iterations, you should have enough proxy signal to answer: "Is this climb producing any movement at all?" If not, the metric or the mutable surface needs redesign — don't burn 100 iterations hoping for signal that isn't there.

### Example evaluation

Objective: "Make the bot's responses more helpful when users are frustrated"

| Candidate | Law 1 (orderable?) | Law 2 (consistent?) | Speed |
|---|---|---|---|
| User satisfaction rating 1-5 | Yes, but coarse | Noisy — same response gets 2-4 depending on mood | Slow (needs users) |
| 4-item binary checklist (acknowledges emotion? offers action? avoids dismissal? stays calm?) | Yes, 16 levels | High — LLM judges are stable on yes/no | Fast (can run offline) |
| Response length delta | Weakly orderable | Very consistent | Immediate |

Pick the candidate that best balances orderability, consistency, and feedback speed. Often this is a coarse-but-stable metric over a fine-but-noisy one.

---

## Step 4: Mutable Surface — What Can the Climber Change?

Define the blast radius. What files, configs, prompts, or data structures is the climber allowed to modify? What is off-limits?

**Questions to answer:**
- What artifact(s) does the climber edit each iteration?
- What is the narrowest scope that could still produce improvement?
- Does the mutable surface live in the same repository as agent state? (If yes, `git revert` is dangerous — see Law 3 in `laws.md`)
- What's the collateral damage if the climber makes a bad edit?

**Scope principle:** Start narrow. A single file is ideal. A single directory is acceptable. Multiple directories or cross-file edits increase the search space and make keep/revert decisions harder. You can always expand scope later if the climb plateaus.

**Watch for indirect surfaces:** The climber might modify files that affect other files. A template that generates config files. A prompt that shapes downstream outputs. A schema change that propagates through a pipeline. Map these indirect effects during design, not during debugging.

---

## Step 5: Mutation Types — What Kinds of Edits Are Possible?

Enumerate the types of changes the climber could make. This is NOT a constraint on the climber — it's reconnaissance for YOU (the designer) to understand the search space.

**Why enumerate?** So you can:
- Assess whether Law 5 (informed search) is satisfied — does the climber have enough context to generate each type?
- Identify indirect mutation paths you might not have considered
- Estimate convergence speed (simple mutations converge faster)

**Common mutation types:**
- Direct edits (change text, values, structure)
- Reordering (change sequence of instructions, priority of rules)
- Addition/removal (add new sections, remove dead weight)
- Parametric (change numeric values, thresholds, weights)
- Structural (reorganize, split, merge)
- Indirect (template injection — edit a template that generates ephemeral files the eval reads)

**Leave the curve open.** Enumerate what you can see, but do NOT restrict the climber to your enumeration. The climber should discover mutation types you didn't anticipate. If you constrain the climber to only the types you listed, you've capped the climb at your own imagination — which defeats the purpose of autonomous optimization. The enumeration is for YOUR understanding of the search space, not for the climber's action space.

---

## Step 6: Candidate Pipeline — What Gets Tried First?

This is Law 5 applied to design rather than climbing. The climber will use Law 5 during the climb to choose mutations. YOU use it during design to choose what order to set things up.

**Questions to answer:**
- Which mutation types have the highest expected impact?
- Which can be evaluated most cheaply?
- Is there a natural ordering (fix obvious problems first, then optimize)?
- Are there dependencies (mutation B only makes sense after mutation A)?

**Pipeline design heuristic:** Rank candidates by (expected impact x confidence) / cost. High-impact, high-confidence, low-cost mutations go first. This isn't the climber's job — the climber explores freely. This is YOUR job as the designer to provide good initial context in program.md so the climber's first iterations aren't wasted on low-value exploration.

**What goes into program.md:** The output of this step is the "Context" section of program.md — prior findings, known failure modes, suspected high-value targets. This is how you transfer your design-phase understanding to the climber without constraining its search.

---

## Putting It Together

After completing all six steps, you should have:

1. A plain-language objective (S5 anchor)
2. An inventory of available signals
3. A selected metric with Law 1 and Law 2 evaluation
4. A defined mutable surface with blast radius assessment
5. A mutation type inventory (for your understanding, not the climber's constraint)
6. A prioritized pipeline that informs program.md's Context section

This is everything pre-flight needs. Run `preflight.md` next — the mechanical checks should pass if your metric design is sound, and the judgment checks should have clear answers because you've thought through S4 maturity and S5 clarity during design.

### Design-to-Preflight Mapping

| Climb Design output | Pre-flight input |
|---|---|
| Selected metric (Step 3) | Law 1 & Law 2 mechanical checks |
| Mutable surface (Step 4) | Law 3 reversibility check, Law 4 scope separation |
| Mutation types + pipeline (Steps 5-6) | Law 5 informed search check |
| Plain-language objective (Step 1) | S5 clarity judgment check |
| Available signals (Step 2) | S4 maturity judgment check (what can you detect?) |
