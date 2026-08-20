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

When a source cannot be read, its count is reported as `unavailable`
and `effective` as `unknown` — never as a number. A failed read is not
evidence of zero posts, and printing `0` there is what makes an outage
look like headroom.

Exit codes:
  0 — check ran, both sources readable (agent reads effective)
  2 — sources diverge by more than 1 (smell worth surfacing)
  3 — the ledger could not be read, so no cap headroom was established;
      treat as "do not post until this is fixed", not as zero posts
  4 — one or more archive files could not be parsed, so the archive count
      is a floor rather than a count; headroom is likewise unestablished

Why this lives as a separate script instead of an option on
`count.py`: the upstream skill rule is "do not maintain a separate
daily counter file for Bluesky caps" — archive cross-check is a
diagnostic on the dispatcher, not a counter. Keeping it separate
preserves `count.py`'s ledger-only purity and lets the agent invoke
the two scripts independently (cap check vs. divergence audit).
"""

from __future__ import annotations

import datetime
import glob
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "ERROR: PyYAML is required. Install with `pip install pyyaml` or\n"
        "use the active venv that includes yaml.\n"
    )
    sys.exit(1)

CAP = 5  # post-class actions per UTC day, per core/70-bluesky-guidelines.md
# Mirrors count.py's EXIT_LEDGER_UNREADABLE: the count it printed is a floor
# because at least one ledger file could not be parsed.
COUNT_LEDGER_UNREADABLE = 3
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


def _archive_is_recognized(data: Any) -> bool:
    """Whether a parsed archive matches a shape we know how to read.

    The mirror of count.py's ledger check, and for the same reason: valid YAML
    can still be an archive we cannot interpret — a bare scalar, a mapping
    with no `dispatch` key, or a `dispatch` holding something other than a
    list of action entries. Each yields no actions for exactly the reason an
    empty archive does, and the file may be the only evidence of a dispatch.

    The legitimately empty shapes are recognized: an absent document (an empty
    file parses to None), an empty list, and an empty `dispatch`. Sibling keys
    alongside `dispatch` are fine — the deployed archives include a few with
    an extra `notifications` key.
    """
    if data is None:
        return True
    if isinstance(data, list):
        return all(isinstance(item, dict) for item in data)
    if not isinstance(data, dict):
        return False
    actions = data.get("dispatch")
    if actions is None:
        return False
    return isinstance(actions, list) and all(isinstance(item, dict) for item in actions)


def count_archive(home: Path, today: str) -> tuple[int, int]:
    """Count committed dispatches today from outbox_archive/.

    Returns ``(count, unreadable)`` — the number of post-class actions
    found, and the number of archive files that could not be parsed.
    A caller that ignores the second value is under-counting silently.

    Archive files are named `<UTC-timestamp>_outbox-bsky.yaml` —
    filename prefix is the timestamp. Filter on the YYYY-MM-DD form
    plus the YYYYMMDD-then-T compact form.
    """
    count = 0
    unreadable = 0
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
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            # One corrupt file must not be read as "no dispatch here" without
            # saying so — an unreported skip under-counts against the cap.
            sys.stderr.write(f"cap_check: unreadable archive {base}: {exc}\n")
            unreadable += 1
            continue
        if not _archive_is_recognized(data):
            sys.stderr.write(
                f"cap_check: unrecognised archive structure in {base} "
                f"(top level is {type(data).__name__}); treating as unreadable\n"
            )
            unreadable += 1
            continue
        # Archive doc shape is either a dict with `dispatch:` key or a
        # bare list. Handle both — older dispatches stored the bare list.
        # An empty file parses to None, which is a real "no dispatch here".
        actions = data.get("dispatch", []) if isinstance(data, dict) else (data or [])
        for entry in actions:
            for verb, payload in entry.items():
                if verb in POST_CLASS_ACTIONS and not (
                    isinstance(payload, dict) and payload.get("dryRun")
                ):
                    count += 1
    return count, unreadable


def count_ledger(home: Path, today: str) -> int | None:
    """Count ledger entries today via the canonical count.py.

    Delegates to `count.py` so the ledger walk stays single-sourced
    there.

    Returns the count, or ``None`` when the delegate could not be run to
    a trustworthy number — a missing or timed-out `count.py`, a nonzero
    exit, output that is not an integer, or a count `count.py` itself
    flagged as a floor because a ledger file would not parse. ``None`` is
    not ``0``: a
    failed read says nothing about how many posts went out today, and
    reporting it as zero is what turned a deleted `count.py` into silent
    headroom against the daily cap.
    """
    import json
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
                "--json",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "MIMIR_HOME": str(home)},
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"cap_check: count.py invocation failed: {exc}\n")
        return None
    if result.returncode == COUNT_LEDGER_UNREADABLE:
        # count.py counted what it could and told us the total is a floor.
        # A floor cannot establish headroom.
        sys.stderr.write(
            f"cap_check: count.py reported unreadable ledger source(s): {result.stderr}"
        )
        return None
    if result.returncode != 0:
        sys.stderr.write(f"cap_check: count.py exited {result.returncode}: {result.stderr}\n")
        return None

    raw = result.stdout.strip()
    try:
        payload = json.loads(raw)
    except ValueError:
        sys.stderr.write(f"cap_check: count.py printed non-JSON output: {raw!r}\n")
        return None
    if not isinstance(payload, dict):
        sys.stderr.write(f"cap_check: count.py returned {type(payload).__name__}, not an object\n")
        return None

    # The status code should already have caught these; checking the structured
    # fields too means a future change that reports damage without changing the
    # exit code cannot quietly reopen the hole.
    if payload.get("unreadable") or payload.get("damagedRecords"):
        sys.stderr.write(
            "cap_check: count.py reported damaged ledger data "
            f"(unreadable={payload.get('unreadable')}, "
            f"damagedRecords={payload.get('damagedRecords')})\n"
        )
        return None

    # No `social-cli-*` state directory means we found no sign the poller has
    # ever run here, so a zero is drawn from nothing. This is deliberately not
    # the same as a directory that exists with no ledger in it yet: the poller
    # framework creates its state directory on first run, independent of any
    # dispatch, so that is a genuine initialised-empty state. Failing closed on
    # it instead would deadlock the first post ever sent, since the ledger only
    # appears after a dispatch. An absent field is treated as zero, which fails
    # closed against an older count.py that cannot report this.
    if not payload.get("stateDirs"):
        sys.stderr.write(
            "cap_check: no social-cli state directory found; no evidence the "
            "poller has run, so today's count cannot be established\n"
        )
        return None

    count = payload.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        sys.stderr.write(f"cap_check: count.py reported a non-integer count: {count!r}\n")
        return None
    return count


LEDGER_UNAVAILABLE = 3
ARCHIVE_UNREADABLE = 4


def main() -> int:
    home = _home()
    today = _today_str()
    archive_count, archive_unreadable = count_archive(home, today)
    ledger_count = count_ledger(home, today)

    suffix = f" archive_unreadable={archive_unreadable}" if archive_unreadable else ""

    # Fail closed on either unreadable source. The archive exists precisely
    # because ledger writes can lag or be skipped, so a readable ledger is not
    # a substitute for an archive we could not parse: the only record of a
    # dispatched post may be in the file that failed to load. Reporting a
    # number there would be the same unverified headroom the ledger path fixes.
    if ledger_count is None or archive_unreadable:
        ledger_field = (
            "ledger=unavailable" if ledger_count is None else f"ledger={ledger_count}"
        )
        print(
            f"today_utc={today} archive={archive_count} "
            f"{ledger_field} effective=unknown / {CAP}{suffix}"
        )
        if ledger_count is None:
            sys.stderr.write(
                "cap_check: ledger unavailable — cap headroom NOT established; "
                "fix count.py before posting\n"
            )
            return LEDGER_UNAVAILABLE
        sys.stderr.write(
            f"cap_check: {archive_unreadable} archive file(s) unparseable — the "
            "archive count is a floor, so cap headroom is NOT established; "
            "repair or remove them before posting\n"
        )
        return ARCHIVE_UNREADABLE

    effective = max(archive_count, ledger_count)
    print(
        f"today_utc={today} archive={archive_count} "
        f"ledger={ledger_count} effective={effective} / {CAP}{suffix}"
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
