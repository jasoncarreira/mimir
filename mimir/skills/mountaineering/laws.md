# The Five Laws of Mountaineering

Every successful climb requires all five laws to hold. Violating any one produces failure — often expensive failure, because the loop burns tokens before the violation becomes apparent.

These laws are not rules to follow. They are conditions that must be true for hill-climbing to converge. Think of them as structural requirements, like gravity or friction — you don't choose to obey them, you design around them.

---

## Law 1: Orderable Outcomes

**The optimizer must be able to say "this is better than that."**

This does not require a single scalar metric. Any of these orderings work:

- **Stochastic ordering** — better on average over N evaluations
- **Pareto improvement** — better on dimension X, not worse on dimension Y
- **Coarse ordering** — binary checklist with 3-6 items (produces 8-64 possible scores)

The floor: the ordering must distinguish signal from noise. If identical outputs receive different scores on repeated evaluation, keep/revert decisions become random walks.

### What violation looks like

A prompt optimization climb uses an LLM judge scoring 1-10 on "overall quality." Run 1: the same prompt scores 7. Run 2: it scores 4. The optimizer keeps a change that was actually neutral and reverts one that was actually good. After 50 iterations, the workspace is no better than where it started — possibly worse. Tokens burned, nothing learned.

### What compliance looks like

The same climb uses a 5-item binary checklist: "Does the output address the question? yes/no. Is reasoning supported by evidence? yes/no. Does it avoid known failure modes? yes/no..." The same prompt scores 4/5 consistently. A genuine improvement moves it to 5/5. The ordering is coarse but stable — the optimizer can distinguish real improvement from noise.

### The key tension

The ordering needs to be relaxed enough to be approachable (you don't need mathematical monotonicity) but rigorous enough to overcome noise (you need to be right about "better" more often than wrong). Finding this balance is one of the harder design decisions in setting up a climb.

---

## Law 2: Measurement Consistency

**The metric must score the same way twice.**

This is distinct from Law 1. Law 1 asks: "Can the metric tell better from worse?" Law 2 asks: "Does it tell the same story on repeated measurement?" An orderable metric that drifts between evaluations is useless — the optimizer chases phantom improvements.

### What violation looks like

An LLM judge evaluates prediction quality on a 1-10 vibes scale. Same prediction, same rubric, three runs: 6, 8, 5. The variance (±1.5) is larger than the improvement the optimizer is trying to detect (±0.3). Every keep/revert decision is dominated by measurement noise, not actual quality changes.

### What compliance looks like

The same evaluation uses binary yes/no questions: "Is the prediction falsifiable? Did it specify a timeframe? Does it reference observable evidence?" LLM judges are nearly deterministic on clear yes/no questions — the same input produces the same answers across runs. Consistency is high enough that a genuine improvement (one more "yes") stands out from noise.

### The practical bar

"Scores the same way twice" is the right level of rigor. Not mathematically perfect reproducibility — but consistent enough that the optimizer isn't lying to itself. A crude-but-consistent metric beats a sophisticated-but-noisy one every time.

---

## Law 3: Safe Exploration

**Failed experiments must be fully reversible — without collateral damage.**

The rollback scope must match the modification scope. If the climber only modifies one data structure, rollback only affects that structure. Memories, journal entries, and other state must be untouchable.

This is what enables the "try one change" pattern. Without reversibility, the optimizer becomes conservative (can't afford to try things that might fail) and convergence slows dramatically. With reversibility, every iteration is a free experiment.

### ⚠️ `git revert` is toxic for stateful agents

If the mutable surface lives in the same repository as memory blocks, journal, and state files, `git revert` nukes everything in that commit — including memories that formed during the experiment. Accidental forgetting for the sake of optimization is worse than no rollback at all.

**Never use `git revert` as a rollback mechanism when agent state lives in git.**

### Rollback values

The right rollback mechanism depends on the climb. These are values to optimize for, not a checklist — each has real tradeoffs:

- **Scope isolation** — rollback touches only what the climber modified. The narrower the blast radius, the safer the experiment. Tradeoff: requires upfront design of the mutable surface boundary.
- **Auditability** — every change is logged, every rollback is traceable. An append-only operation log lets you replay history, not just undo it. Tradeoff: storage grows, and replay logic adds complexity.
- **Graceful degradation** — failed experiments degrade quality rather than break things. Soft weights that decay, or additive-only changes that dilute rather than corrupt. Tradeoff: slower convergence, since bad changes linger rather than being cleanly removed.
- **Simplicity** — the simplest mechanism you can get away with. Sometimes "just restore the previous version of one file" is all you need. Tradeoff: only works when the mutable surface is truly isolated to one artifact.

### What violation looks like

A stateful agent uses `git revert` to undo a bad change to its workspace. The revert also removes three journal entries, a memory block update, and a state file change that happened in the same commit window. The agent "forgets" context from the experiment period. The climb optimized one thing at the cost of agent coherence.

### What compliance looks like

The climber writes proposed changes to an isolated workspace file. Each iteration, the evaluator scores the result with the changes applied. If the score drops, the workspace file is rolled back to its previous version. Journal, memory, and state files are never in the rollback path. Failed experiments are invisible to everything except the workspace.

### Connection to scope separation

Safe exploration and scope separation (Law 4) are spiritually related — both are about containment. Law 3 contains damage (failed experiments can be undone). Law 4 contains influence (the optimizer can't redefine success). Two faces of the same structural constraint: the optimizer's reach must be bounded.

---

## Law 4: Scope Separation

**The optimizer must not control the evaluation.**

The moment an agent can edit its own success criteria, "improvement" becomes circular. This is the most load-bearing constraint in the entire system — without it, the climb converges on whatever is easiest to score well on, not what actually matters.

### What violation looks like

A prediction-improvement climb gives the climber write access to both the prediction memory blocks AND the evaluation rubric. After 20 iterations, the climber has subtly narrowed the rubric to reward the kinds of predictions it's already good at. Scores go up. Actual prediction quality is flat or declining. The metric looks great; the work is worthless.

### What compliance looks like

The evaluation logic is held in the supervisor's memory, loaded at climb registration time. Each iteration, the supervisor passes the frozen evaluation to the climber as read-only context. The climber literally cannot see or touch the evaluation files on disk — they exist only in the supervisor's process memory. Architectural enforcement ("don't give them the lock") rather than detective enforcement ("verify the lock wasn't picked").

### Enforcement hierarchy

1. **Architectural** (strongest) — evaluation lives in-process in the supervisor, never on disk where the climber could reach it
2. **Structural** — frozen files in a hidden directory, diffed each iteration
3. **Detective** — post-hoc analysis of whether scoring patterns shifted

Design for level 1. Fall back to level 2 if the evaluation must execute as a script. Level 3 is a diagnostic tool, not a prevention mechanism.

---

## Law 5: Informed Search

**The optimizer must have domain knowledge sufficient to generate targeted hypotheses.**

This is what distinguishes mountaineering from random search or grid search. An LLM reads failing cases and hypothesizes specific fixes. That's why a well-set-up climb converges in 4-20 iterations, not 4000 — the proposals are informed, not random.

### What violation looks like

A code optimization climb provides the climber with the source file and the performance benchmark, but not the profiling output or the known bottleneck analysis. The climber makes random-seeming changes — reformatting code, renaming variables, adding comments — because it has no signal about what's actually slow. After 100 iterations, performance hasn't changed. The climb was expensive random search.

### What compliance looks like

The same climb includes profiling output in the program.md context: "Function X accounts for 80% of runtime. Prior attempts to optimize it via approach Y failed because Z." The climber's first proposal targets function X with an approach informed by the failure mode. Convergence happens in 5-10 iterations because each proposal is a targeted hypothesis, not a guess.

### The deeper point

Law 5 is why mountaineering is an LLM-native pattern, not just automated A/B testing. The value of the LLM isn't generating random variations — it's reading the current state, understanding why it's failing, and proposing a specific fix. Without this, you don't need an LLM; a random mutator would work equally well (and cost less).
