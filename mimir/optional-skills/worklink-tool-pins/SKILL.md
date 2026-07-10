---
name: worklink-tool-pins
description: "Optional low-priority poller that inventories Worklink tool pins from <home>/worklink.yaml and files/reuses Chainlink bump issues when upstream versions drift. Opt-in: copy this directory into <home>/skills/worklink-tool-pins/ and configure tool_pins in worklink.yaml."
env:
  optional:
    - name: MIMIR_HOME
      description: "Agent home containing worklink.yaml and the Chainlink tracker. Supplied by the framework when it launches the poller; the poller refuses to run (emits worklink_tool_pins_misconfigured) if unset — no container-path fallback."
      example: "/path/to/agent-home"
---

# worklink-tool-pins — Worklink external-tool drift poller

This is an **opt-in poller skill** that ships under
`mimir/optional-skills/` but is NOT auto-installed. It watches the
`tool_pins:` section of `<home>/worklink.yaml` and files or reuses
low-priority Chainlink bump issues when an upstream version differs
from the configured pin.

## What it does

- Loads the documented `tool_pins:` section from `<home>/worklink.yaml`
  with a standalone stdlib parser. Poller subprocesses run outside
  mimir's venv/import path, so this skill intentionally does not import
  `mimir.*` or PyYAML at runtime.
- Inventories configured `tool_pins:` against supported upstream
  sources (`npm` and GitHub release sources).
- Files or reuses low-priority Chainlink issues carrying the stable
  `Dedupe-Key: worklink-tool-pin:<category>:<name>:<old>-><new>`.
- Emits JSONL only when at least one bump issue was filed or reused.

## What it does not do

- It does **not** auto-edit `worklink.yaml`.
- It does **not** run smoke commands. Smoke commands are copied into
  the Chainlink issue as the suggested validation command for the
  eventual bump PR.
- It does **not** alert on missing config or no drift; silence is the
  expected healthy path.

## Installation

```bash
cp -r mimir/optional-skills/worklink-tool-pins <home>/skills/
# Ensure MIMIR_HOME points at the home containing worklink.yaml.
reload_pollers
```

The shipped cadence is weekly Sunday 08:00 UTC with low priority so it
is shed before interactive or high-value pollers under resource pressure.

## `worklink.yaml` shape

```yaml
tool_pins:
  - name: codex
    category: coding-cli
    pin: "0.144.1"
    smoke: "codex --version"
    source: npm
    package: "@openai/codex"
  - name: chainlink
    category: tracker
    pin: "chainlink-1.6.0"
    smoke: "chainlink --version"
    source: github-release
    repo: dollspace-gay/chainlink
```

Supported `source` values:

- `npm` — resolves `package` (or `name`) via `npm view <package> version`.
- `github-release`, `github`, or `github-tag` — resolves `repo` via
  `gh release view --repo <owner/repo> --json tagName`.

Pins with `source: manual` / `local`, missing resolvers, or resolver
failures are skipped as diagnostics on stderr; they do not make the
poller non-zero.

The parser is intentionally narrow rather than a full YAML interpreter:
it supports the documented top-level `tool_pins:` list of scalar
mappings. If the Worklink config format grows nested tool-pin fields,
update this poller parser and its subprocess smoke test in the same PR.
