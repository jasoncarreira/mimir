#!/usr/bin/env python3
"""Cross-source Bluesky post-cap check (archive + ledger).

The primary cap check is `count.py` — it walks `sent_ledger-*.yaml`
files across `social-cli-*` poller state dirs and counts post-creating
actions for the UTC window. `count.py` is ledger-only because that's
the surface the upstream `social-cli` binary owns.

This helper adds a cross-check against `outbox_archive/` — the
durable record of dispatched outbox YAMLs — because ledger writes
can lag, fail, or be skipped (race conditions, partial archive
writes, dispatcher quirks). The archive is written *before*
dispatch by `dispatch-outbox.sh`, so archive presence is a strong
"this was attempted today" signal even when the ledger is empty.

The two sources usually agree. When they diverge by more than one,
the script exits non-zero and prints both counts — the agent reads
the divergence as a smell worth filing, not a hard error.

Usage (from any cwd):

    python3 skills/social-cli/scripts/cap_check.py
    MIMIR_HOME=/mimir-home python3 skills/social-cli/scripts/cap_check.py

Output (one line):

    today_utc=YYYY-MM-DD archive=N ledger=M effective=K / 5

Exit codes:
  0 — check ran (regardless of effective value; agent reads effective)
  2 — sources diverge by more than 1 (smell worth surfacing)

Why this lives as a separate script instead of an option on
`count.py`: the upstream skill rule is "do not maintain a separate
daily counter file for Bluesky caps" — archive cross-check is a
diagnostic on the dispatcher, not a counter. Keeping it separate
preserves `count.py`'s ledger-only purity and lets the agent invoke
the two scripts independently (cap check vs. divergence audit).
"""

import datetime
import glob
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "ERROR: PyYAML is required. Install with `pip install pyyaml` or\n"
        "use the active venv that includes yaml.\n"
    )
    sys.exit(1)

CAP = 5  # post-class actions per UTC day, per core/70-bluesky-guidelines.md
POST_CLASS_ACTIONS = {"post", "reply", "thread", "quote"}


def _home() -> Path:
    """Resolve MIMIR_HOME so the script works regardless of cwd."""
    # parents[3] walks up from <home>/skills/social-cli/scripts/cap_check.py.
    # This script lives beside count.py in scripts/ so the delegation below
    # resolves; the depth here has to match that placement.
    return Path(os.environ.get("MIMIR_HOME") or Path(__file__).resolve().parents[3])


def _today_str() -> str:
    """UTC today in YYYY-MM-DD form (matches archive filename date key)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def count_archive(home: Path, today: str) -> int:
    """Count committed dispatches today from outbox_archive/.

    Archive files are named `<UTC-timestamp>_outbox-bsky.yaml` —
    filename prefix is the timestamp. Filter on the YYYY-MM-DD form
    plus the YYYYMMDD-then-T compact form.
    """
    count = 0
    pattern = str(
        home / "state" / "pollers" / "social-cli-*"
        / "outbox_archive" / "*_outbox-bsky.yaml"
    )
    date_prefix_compact = today.replace("-", "")  # YYYYMMDD
    for archive in glob.glob(pattern):
        base = os.path.basename(archive)
        # Match either YYYY-MM-DDTHH... or YYYYMMDDTHH...; the dispatch
        # wrapper uses YYYY-MM-DDTHH-MM-SS-mmmZ form (hyphens inside the
        # time component).
        if not (base.startswith(today + "T") or base.startswith(date_prefix_compact + "T")):
            continue
        try:
            with open(archive) as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        # Archive doc shape is either a dict with `dispatch:` key or a
        # bare list. Handle both — older dispatches stored the bare list.
        actions = data.get("dispatch", []) if isinstance(data, dict) else data
        if not isinstance(actions, list):
            continue
        for entry in actions:
            if not isinstance(entry, dict):
                continue
            for verb, payload in entry.items():
                if verb in POST_CLASS_ACTIONS and not (
                    isinstance(payload, dict) and payload.get("dryRun")
                ):
                    count += 1
    return count


def count_ledger(home: Path, today: str) -> int:
    """Count ledger entries today via the canonical count.py.

    Delegates to `count.py` so the ledger walk stays single-sourced
    there. Imports dynamically to avoid pulling count.py's argparse
    setup overhead at module load.
    """
    import subprocess

    count_script = Path(__file__).resolve().parent / "count.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(count_script),
                "--platform", "bsky",
                "--action", "post",
                "--since", today,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "MIMIR_HOME": str(home)},
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"cap_check: count.py invocation failed: {exc}\n")
        return 0
    if result.returncode != 0:
        sys.stderr.write(f"cap_check: count.py exited {result.returncode}: {result.stderr}\n")
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def main() -> int:
    home = _home()
    today = _today_str()
    archive_count = count_archive(home, today)
    ledger_count = count_ledger(home, today)
    effective = max(archive_count, ledger_count)
    print(
        f"today_utc={today} archive={archive_count} "
        f"ledger={ledger_count} effective={effective} / {CAP}"
    )
    # Smell threshold: the two sources diverge by more than 1. Don't
    # treat divergence as a hard error — both surfaces can be valid in
    # different ways — but exit non-zero so the agent records the
    # divergence as a follow-up signal without auto-dispatching.
    if abs(archive_count - ledger_count) > 1:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
