"""Fold per-change changelog fragments into a versioned release section."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re

POINTER = "Changes awaiting release are recorded in [changelog.d/](changelog.d/)."
ROOT = Path(__file__).resolve().parents[1]


def collect(root: Path, version: str, release_date: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must be X.Y.Z")
    if date.fromisoformat(release_date).isoformat() != release_date:
        raise ValueError("date must be YYYY-MM-DD")

    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    match = re.search(r"(?m)^## \[Unreleased\]\n", text)
    if match is None:
        raise ValueError("CHANGELOG.md has no ## [Unreleased] heading")
    next_heading = re.search(r"(?m)^## \[", text[match.end():])
    end = match.end() + next_heading.start() if next_heading else len(text)
    before, block, after = text[:match.start()], text[match.end():end], text[end:]
    if re.search(rf"(?m)^## \[{re.escape(version)}\]", after):
        raise ValueError(f"CHANGELOG.md already has a {version} section")

    legacy = "".join(line for line in block.splitlines(keepends=True)
                     if line.rstrip("\r\n") != POINTER).strip("\n")
    fragments = sorted(path for path in (root / "changelog.d").glob("*.md")
                       if path.name != "README.md")
    entries = []
    for path in fragments:
        entry = path.read_text(encoding="utf-8")
        if not re.match(r"[-*+]\s", entry):
            raise ValueError(f"{path.name}: fragment must start with a Markdown bullet")
        entries.append(entry)
    if legacy:
        entries.append(legacy)
    if not entries:
        raise ValueError("nothing to collect: no fragments or legacy Unreleased entries")

    def separated(entry: str) -> str:
        # Keep every byte of the entry, adding only the missing section separator.
        return entry + ("" if entry.endswith("\n\n") else "\n" if entry.endswith("\n") else "\n\n")

    section = f"## [{version}] — {release_date}\n\n" + "".join(map(separated, entries))
    changelog.write_text(
        before + f"## [Unreleased]\n\n{POINTER}\n\n" + section + after,
        encoding="utf-8",
    )
    for path in fragments:
        path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version X.Y.Z")
    parser.add_argument("--date", default=date.today().isoformat(), help="release date YYYY-MM-DD")
    args = parser.parse_args()
    try:
        collect(ROOT, args.version, args.date)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"changelog collection failed: {exc}\n")


if __name__ == "__main__":
    main()
