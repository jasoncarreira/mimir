---
name: tool-pin-drift
description: Check pinned external tool versions against npm and GitHub releases. Use for the weekly heartbeat tool-pin drift scan.
success_criteria:
  tool_call:
    name: Bash
    args:
      command_glob: "python3 */.mimir_builtin_skills/tool-pin-drift/check_tool_pins.py"
---

<!-- desc: Run the immutable, data-driven npm and GitHub tool-pin drift checker and consume its per-target JSON results. -->

# Tool Pin Drift

Checks the source-controlled targets in `targets.json` and emits one JSON
document. Each target reports `pinned_version`, `latest_version`, `drifted`,
`status`, and `error`; one failed lookup does not prevent the remaining targets
from running.

## Invocation

The deployment declares and invokes exactly this command, with no arguments,
shell operators, pipes, or redirection:

```bash
python3 /mimir-home/.mimir_builtin_skills/tool-pin-drift/check_tool_pins.py
```

The corresponding `scheduler.yaml` declaration is:

```yaml
shell_commands:
  - exec: python3
    path: /usr/bin/python3
    script: /mimir-home/.mimir_builtin_skills/tool-pin-drift/check_tool_pins.py
```

The script intentionally runs `npm` and `gh` as ordinary subprocesses. Those
commands must not be added to the shared `maintenance` shell profile. Declaring
this immutable builtin script grants its subprocess behavior only to the job
whose operator-owned scheduler entry names it.

## Output

`status` is `drift`, `no_drift`, or `error`. On an error, `latest_version` and
`drifted` are `null`, and `error` contains the per-target failure. The process
still checks every other target and exits successfully after emitting the full
result.
