# Security policy

## Supported versions

mimir is pre-1.0. Only the latest release on PyPI (`mimir-agent`) receives
security fixes. Older versions should be upgraded.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use GitHub's private vulnerability reporting:
<https://github.com/jasoncarreira/mimir/security/advisories/new>

Or email the maintainer privately. Include:

- A description of the issue and its impact
- Steps to reproduce (or a proof-of-concept if appropriate)
- The version of mimir / commit SHA you tested against
- Any suggested mitigation

Expect an acknowledgement within 7 days. Coordinated disclosure timelines are
negotiated case by case; the default is 90 days from acknowledgement to
public disclosure, shorter for actively-exploited issues.

## Threat model — known posture

mimir is designed to run **inside** an operator's trust boundary (their
laptop, their server, their container). The HTTP server is intended to be
reachable only by the operator and their bridges. Defaults reflect that:

- The HTTP server binds to `127.0.0.1` by default. To bind a non-loopback
  interface (`0.0.0.0`, a specific external IP), set `MIMIR_API_KEY` and
  pass `--host` explicitly. Binding `0.0.0.0` without `MIMIR_API_KEY` is
  refused at startup.
- Skills, tools, and the agent itself can read and write files in the agent
  home, run shell commands, and make outbound HTTP calls. Treat the agent
  home as you would any process with full local user privileges.
- The agent does not have privilege separation between skills; a malicious
  skill installed in `~/<agent-home>/.claude/skills/` can do anything the
  agent process can do. Curate the skill set.
- Outbound network calls (PyPI version-check, model-provider APIs, skill
  pollers) are not sandboxed; use OS-level egress filtering if that matters
  to your deployment.
- Subprocess-based skills (`spawn_claude_code`) inherit the agent process's
  environment. Sensitive environment variables propagate.

If you operate mimir outside this model — multi-tenant, exposed to the
public internet, etc. — you are operating outside the supported posture and
should expect additional hardening on your side.

## Known limitations

These are documented because the code-level guards are weaker than the docs
might initially suggest:

- `mimir/tools/prohibited_action_guard.py` is a *regex check* on the
  command string, not a sandbox. A determined caller wraps the command in
  a script file and gets past it. Treat it as best-effort surface-area
  reduction, not a security boundary.
- DNS-rebinding mitigation for the web-fetch tool is documented in code
  but not currently enforced. Don't point the agent at an untrusted URL
  on a network where it shouldn't be able to reach internal IPs.
- Bash output redaction (`mimir/git_bootstrap.py` `_redact`) covers
  common token patterns but is not exhaustive. Avoid `env` / printenv-
  style commands in production agent homes that auto-commit `turns.jsonl`.

## Out of scope

- Bugs that require operator access to the agent home to exploit
- Denial-of-service via legitimate-but-expensive agent prompts (quota is
  the right control)
- Behaviors that are intentional and documented above
