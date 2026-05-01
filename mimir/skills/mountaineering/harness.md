# Harness Setup

The harness is the structural scaffolding that enforces the five laws during a climb. Set this up once before starting the loop.

## Directory Structure

Each climb lives in its own directory:

```
climbers/
└── {climb-id}/
    ├── program.md          # Frozen S5 — goal, constraints, scope
    ├── eval/               # Evaluation scripts and rubrics
    │   ├── eval.py         # Scoring script
    │   └── rubric.md       # Evaluation criteria (if LLM-judged)
    ├── workspace/          # The mutable surface — what the climber can edit
    │   └── (whatever the climb targets)
    ├── logs/               # Append-only results
    │   └── results.jsonl   # One entry per iteration (ring buffer)
    └── config.json         # Climb configuration
```

**Note:** There is no `.frozen/` directory. Evaluation files are loaded into the supervisor's process memory at climb registration time. The climber never has the eval files on disk in its workspace — architectural enforcement of Law 4 (scope separation).

## config.json

```json
{
  "climb_id": "my-climb",
  "max_iterations": 500,
  "results_window": 20,
  "eval_command": "python eval/eval.py",
  "scope": ["workspace/"],
  "budget_limit_tokens": 1000000
}
```

Key fields:
- `results_window` — how many past results the climber sees each iteration (prevents context growth)
- `scope` — directories/files the climber is allowed to modify (everything else is read-only)
- `eval_command` — command the supervisor runs to score the workspace (the climber never runs this directly)

## program.md Template

This is the climber's frozen identity. Write it before the climb starts. It doesn't change during the climb.

```markdown
# Climb: {name}

## Goal
{What are we optimizing? Be specific.}

## Metric
{How do we measure improvement? Reference the eval approach.}

## Scope
{What can the climber modify? What is off-limits?}

## Constraints
- One change per iteration
- Must be reversible (git commit before each change)
- Do not modify files outside workspace/

## Context
{Domain knowledge the climber needs. Background, prior findings,
known failure modes. This is Law 5 — informed search.}

## Stop Conditions
- Plateau: no improvement for {N} consecutive iterations
- Budget: {max_iterations} iterations or {token_limit} tokens
- External: supervisor kills the climb
```

## Evaluation Patterns

The eval script scores the current state of the workspace. The supervisor runs this — the climber never executes it directly (Law 4).

### Deterministic Eval

For metrics that can be computed programmatically:

```python
#!/usr/bin/env python3
"""Evaluation script for {climb-name}."""
import json
import sys

def evaluate():
    # Read the current state of the workspace
    # ...

    # Score it
    score = 0.0  # Replace with actual scoring logic
    details = {}  # Optional breakdown

    return {
        "score": score,
        "details": details,
        "pass": score > THRESHOLD,
    }

if __name__ == "__main__":
    result = evaluate()
    print(json.dumps(result))
    sys.exit(0)
```

Requirements:
1. Deterministic output (Law 2)
2. JSON result to stdout
3. Exit 0 on success, non-zero on eval error (not low score — actual failure)

### LLM-Judge Eval

For metrics that need LLM judgment (text quality, insight generation). Use binary yes/no questions for consistency (Law 2):

```python
#!/usr/bin/env python3
"""LLM-judge evaluation. Uses a SEPARATE model call for scope separation."""
import json
import sys

def evaluate():
    with open("workspace/output.txt") as f:
        output = f.read()

    with open("eval/rubric.md") as f:
        rubric = f.read()

    # Call LLM judge — NOT the same model doing the climbing
    # Binary yes/no checklist for consistency (Law 2)
    checks = [
        judge(output, rubric, "Does the output address the core question? yes/no"),
        judge(output, rubric, "Is the reasoning supported by evidence? yes/no"),
        judge(output, rubric, "Does it avoid the known failure modes? yes/no"),
    ]

    score = sum(1 for c in checks if c == "yes") / len(checks)
    return {"score": score, "checks": checks}
```

### Judge Panel

For stronger Law 4 enforcement — multiple judges seeded with different perspectives so they genuinely disagree:

```python
JUDGES = [
    {"name": "accuracy", "prompt": "Is this factually correct and well-calibrated?"},
    {"name": "breadth", "prompt": "Does this cover diverse aspects, not just easy ones?"},
    {"name": "surprise", "prompt": "Does this contain insights not obvious from priors?"},
    {"name": "adversarial", "prompt": "Is this trivially derivable? Could it be gamed?"},
]

def evaluate():
    scores = {}
    for judge in JUDGES:
        scores[judge["name"]] = run_judge(judge["prompt"], workspace_output)
    # Convergence across disagreeing judges = real signal
    return {"score": mean(scores.values()), "judges": scores}
```

The adversarial judge is key — it specifically checks for gaming patterns. Convergence across all four judges (including the adversarial one) is stronger evidence of genuine improvement than any single judge.

## Results Log Format

Each iteration appends one line to `logs/results.jsonl`. The log operates as a ring buffer — the climber only reads the last `results_window` entries each iteration:

```json
{
  "iteration": 42,
  "timestamp": "2026-03-19T14:30:00Z",
  "change": "Modified workspace/prompt.md line 12: added specificity constraint",
  "score": 0.75,
  "previous_score": 0.70,
  "decision": "keep",
  "details": {"accuracy": 0.8, "breadth": 0.7, "surprise": 0.75}
}
```

The log file grows, but memory usage does not — the climber reads only the last N entries via streaming (deque with maxlen).

## Common Harness Patterns

### Prompt Optimization
- Workspace: single markdown file (the prompt)
- Eval: LLM judge with binary checklist
- Scope: one file, easy to diff
- Expected convergence: 4-20 iterations

### Config Tuning
- Workspace: config file (JSON/YAML)
- Eval: run the system, measure output quality
- Scope: config values only, not code
- Expected convergence: 10-50 iterations

### Code Optimization
- Workspace: source file(s)
- Eval: test suite + performance benchmark
- Scope: implementation only, not tests
- Coding agent option: for sophisticated file manipulation, consider using `npx acpx` or similar coding agent as the climber runtime instead of the default DeepAgent. Document this as a power-user configuration, not the default.
- Expected convergence: 10-100 iterations

### World Model (Predictions)
- Workspace: memory block content (natural language)
- Eval: prediction accuracy + insight generation (judge panel)
- Scope: memory blocks, not prediction resolution logic
- Iteration cycle: weekly (slow feedback — predictions take days/weeks to resolve)
- Expected convergence: 10-20 cycles (months)
- Special consideration: interleaved memory updates between iterations can be filtered by having the patch-applying agent examine other changes for confounds
