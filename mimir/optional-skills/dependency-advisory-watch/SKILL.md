---
name: dependency-advisory-watch
description: "Scans lockfiles (uv.lock, package-lock.json) for known vulnerabilities using the OSV API. Inventories Python packages from uv.lock (PyPI) and npm packages from package-lock.json, queries OSV for vulnerabilities, and emits deterministic JSONL records for matched advisories."
env:
  optional:
    - name: ROOT_DIR
      description: "Repository root (default: cwd)"
      example: "/path/to/repo"
    - name: OSV_API_URL
      description: "OSV API endpoint (default: https://api.osv.dev/v1/querybatch)"
      example: "https://api.osv.dev/v1/querybatch"
---

# Dependency Advisory Watch

Scans lockfiles (uv.lock, package-lock.json) for known vulnerabilities using the OSV API.

## Functionality

- Inventories Python packages from `uv.lock` (PyPI)
- Inventories npm packages from `package-lock.json`
- Queries OSV API for each package's vulnerabilities
- Emits deterministic JSONL records for matched advisories

## Environment

- `ROOT_DIR`: Repository root (default: cwd)
- `OSV_API_URL`: OSV API endpoint (default: https://api.osv.dev/v1/querybatch)

## Output

Each vulnerability emits a JSON record:
```json
{
  "package": "aiohttp",
  "current_version": "3.13.5",
  "affected_range": ">=3.9.0,<3.9.2",
  "severity": "7.5",
  "advisory_url": "https://osv.dev/vulnerability/...",
  "remediation_version": "3.9.2"
}
```

## Contract

- stdout: JSONL events (one per vulnerability)
- stderr: errors only
- exit 0: success (no vulnerabilities is OK)
- exit non-zero: OSV transport/parse failure
