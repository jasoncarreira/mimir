---
name: dependency-advisory-watch
description: "Scans root uv.lock and package-lock.json files with the pinned osv-scanner CLI and emits deterministic advisory records."
env:
  optional:
    - name: ROOT_DIR
      description: "Repository root (default: cwd)"
      example: "/path/to/repo"
    - name: OSV_SCANNER
      description: "osv-scanner executable (default: osv-scanner)"
      example: "/usr/local/bin/osv-scanner"
---

# Dependency Advisory Watch

Runs the pinned `osv-scanner` CLI against supported lockfiles at the repository
root. `osv-scanner` owns lockfile parsing and OSV transport/batching; this skill
only normalizes its JSON output into stable poller events.

## Output

Each vulnerability emits one JSON object with `package`, `current_version`,
`affected_range`, `severity`, `advisory_url`, and (when available)
`remediation_version`. Records are sorted deterministically and duplicates are
removed.

## Contract

- stdout: JSONL events, one per vulnerability
- stderr: scanner/JSON errors only
- exit 0: completed scan, including a completed scan with findings
- exit 2: executable, scanner transport/parse, or scanner JSON failure

`osv-scanner` itself exits 1 when it finds vulnerabilities. The wrapper parses
that output and exits 0 after emitting the findings; only scanner exit codes
above 1 are failures. Tests mock the subprocess and never use the network.
