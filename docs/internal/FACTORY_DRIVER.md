# Feature-factory epic driver (chainlink #834)

Runs the external opencode **feature-factory** autonomously inside the mimir
agent so an operator only has to label an epic. The factory is
chainlink-agnostic; `mimir/worklink/factory.py` is the sole adapter.

## Flow

```
worklink:epic + worklink:ready issue
  → poller (opt-in) dispatches: mimir worklink factory <id> --autonomous
  → FactoryEpicRunner:
      claim epic
      feature-factory factory start --headless --repo <repo> "<epic prompt>"
      loop, reading .opencode/factory/chainlink-<id>/run.json:
        story  gate → approve      (trust the factory's own validator)
        brief  gate → approve      (trust the factory's own validator)
        pre_pr gate → independent review subagent:
                        no_concerns|nits → approve
                        important        → changes: <rationale>  (factory re-gates)
                        blocker          → stop  (epic → blocked)
        resume until terminal
      on the factory's draft PR: gh pr ready + gh pr edit --add-reviewer mimir-carreira
      transition epic → worklink:review
```

### Gate policy (decided 2026-07-04)

`story`/`brief` are auto-approved on the factory's own validator — trusted
autonomously. `pre_pr` has **no human gate**; an independent review subagent
reads the diff, runs the suite, and returns approve/changes/stop. **The PR is the
human review point** — it opens with the mimir reviewer requested and nothing
auto-merges. The `pre_pr` reviewer fails *safe*: an unparseable verdict maps to
`changes`, never a silent approve.

## Enabling it

Opt-in and gated on the factory being installed in the deployment image:

| Env | Meaning |
|---|---|
| `MIMIR_FACTORY_EPICS_ENABLED` | `true` to let the poller dispatch epics (default off). |
| `MIMIR_FEATURE_FACTORY_BIN` | `feature-factory` CLI invocation (shlex-split; e.g. `node /opt/ff/src/cli.js`). Default `feature-factory`. |
| `MIMIR_FACTORY_REVIEWER` | GitHub login requested on the PR. Default `mimir-carreira`. |

All are threaded through the poller's `pass_env` allowlist.

## Deploy steps (operator image — not in this PR's testable code)

These land as image/config changes on the mimirbot/muninn deployments:

1. **Install the factory in the image** — the `feature-factory` CLI + plugin
   (`src/plugin.js`) + `assets/` (11 agents + `/feature` skill), wired into the
   container's `~/.config/opencode/opencode.jsonc`. (Repo-local `SCHEMA.md`
   seeding on `factory start`, `external_directory: deny`, and agent-frontmatter
   `bash: allow` are handled factory-side.)
2. **Deployment model routing** — the factory's per-role model+variant map must
   target the deployment's providers/auth, **not** a host's `openai/gpt-5.x`
   (mimirbot = codex-plus, muninn = minimax/anthropic). Verify all 11 agents
   resolve with `feature-factory doctor --local --provider-smoke`.
3. **In-container checkout** — a writable git checkout with worktree support for
   `--repo` (mimirbot: `/workspace/mimir`).

The agent's own GH account (mimir-carreira) can push in-container, so the
factory opens the PR directly; the local `jason-visotrust`-vs-`jasoncarreira`
403 seen when driving from a laptop does not occur there.

## Headless robustness

A headless factory run wedges forever on any permission `ask` (no responder).
The factory's own agents register non-interactive (`bash: allow`,
`external_directory: deny`); confirm with `feature-factory doctor` before
enabling. Wedge symptom in `~/.local/share/opencode/log/opencode.log`: a
`message=asking` line with no activity after it.

## Testing

`tests/test_worklink_factory.py` drives the full gate loop deterministically —
the factory CLI is simulated by a stateful fake runner that advances `run.json`
on each `start`/`resume` — covering the happy path, the `pre_pr` changes loop,
`blocker`→stop, claim-declined, terminal-without-PR, and the poller routing.
