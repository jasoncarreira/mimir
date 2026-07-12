---
name: dependency-advisory-watch
description: "Scans root uv.lock and package-lock.json files with the pinned osv-scanner CLI and emits deterministic advisory records. Opt-in: copy this directory into <home>/skills/dependency-advisory-watch/ and install osv-scanner."
env:
  optional:
    - name: ROOT_DIR
      description: "Repository root to scan (default: cwd). Must contain uv.lock and/or package-lock.json at the root."
      example: "/path/to/repo"
    - name: OSV_SCANNER
      description: "osv-scanner executable (default: osv-scanner). Must be installed and in PATH or an absolute path."
      example: "/usr/local/bin/osv-scanner"
---

# Dependency Advisory Watch

<!-- desc: Scans Python and npm lockfiles for OSV vulnerabilities and emits dependency_advisory events -->

This is an **opt-in poller skill** that ships with mimir under
`mimir/optional-skills/` but is NOT auto-installed. It monitors your
project's dependencies for known vulnerabilities using OSV (Open Source
Vulnerabilities).

## Installation

1. **Install osv-scanner v2.4.0** — the poller invokes the v2-only `scan`
   subcommand. The shipped `dockerfile.fragment` is the canonical pinned
   container installation and supports `amd64` and `arm64`. For a direct
   installation on Linux:
   ```bash
   # Option A: pinned binary release (set amd64 or arm64)
   OSV_SCANNER_ARCH=amd64
   curl -fsSL \
     "https://github.com/google/osv-scanner/releases/download/v2.4.0/osv-scanner_linux_${OSV_SCANNER_ARCH}" \
     -o /usr/local/bin/osv-scanner
   chmod 0755 /usr/local/bin/osv-scanner

   # Option B: pinned Go install
   go install github.com/google/osv-scanner/v2/cmd/osv-scanner@v2.4.0
   ```

2. **Copy the skill to your agent home:**
   ```bash
   cp -r mimir/optional-skills/dependency-advisory-watch <home>/skills/
   ```

3. **Configure (optional):**
   - `ROOT_DIR` — path to the repository to scan (default: current working directory)
   - `OSV_SCANNER` — path to osv-scanner binary (default: `osv-scanner` from PATH)

4. **Bring it live:**
   ```bash
   reload_pollers
   # → "reload_pollers ok: N poller(s) registered — dependency-advisory-watch, ..."
   ```

## Cadence

Runs at **06:00 UTC daily** (`0 6 * * *`). This bounded UTC cadence provides
a daily morning scan at a consistent time regardless of agent timezone.

## State & Cursor Behavior

The poller uses a cursor file (`dependency-advisory-cursor.json`) in the
framework-injected `STATE_DIR` to track seen advisory IDs:

- **First run**: seeds the cursor with current advisories but emits NO events
  (the agent isn't woken for pre-existing vulnerabilities)
- **Subsequent runs**: emits one event per NEW advisory since last scan
- **Resolved advisories**: when an ID disappears from the scan, it's removed
  from the cursor silently
- **Reappearing advisories**: if a previously-seen ID reappears (e.g. downgrade),
  it's emitted as a new event

Cursor updates are atomic (written to temp file then renamed) and only occur
after a fully successful scan. On failure, the prior cursor is preserved.

## Clean-Scan Silence

A scan with **no new vulnerabilities** produces zero events. The poller exits 0
and logs `poller_complete{events_emitted=0}` to the events log. This provides
low-noise operation — you only hear about new problems.

If vulnerabilities ARE found, each emits one event with:
- `package`, `current_version`, `affected_range`, `severity`
- `advisory_url` (links to OSV.dev)
- `remediation_version` (if available)

## Failure Observability

| Exit Code | Meaning | Event |
|-----------|---------|-------|
| 0 | Scan completed (with or without findings) | `poller_complete` |
| 2 | Scanner executable missing, transport error, or JSON parse failure | `poller_nonzero_exit` with stderr |

Diagnostic output from the scanner (errors, warnings) goes to stderr and is
captured as `poller_stderr` events for debugging.

## OSV Coverage Boundary

This poller scans Python and npm lockfiles:
- `uv.lock` (Python/uv)
- `package-lock.json` (JavaScript/npm)

Coverage is limited to what OSV.dev supports for these lockfile formats.
The poller does NOT scan:
- Dockerfiles or container images
- Git dependencies or submodules
- System packages
- Other language ecosystems (even if present in mixed repos)

The skill normalizes osv-scanner's JSON output into deterministic,
sorted events with stable IDs for reliable cursor tracking.

## Security

- **No static secrets** — the skill has no required or optional secrets
- **No credential files** — operates entirely on public OSV data
- **`ROOT_DIR` configuration** — repository path is explicit via env var,
  not hardcoded
