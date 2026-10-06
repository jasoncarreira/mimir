"""Referenced Python call sites in the feedback guide must still exist."""

from pathlib import Path
import re


def test_feedback_loop_python_paths_exist():
    root = Path(__file__).resolve().parents[1]
    text = (root / "FEEDBACK-LOOPS.md").read_text()
    paths = set(re.findall(r"mimir/[\w/-]+\.py\b", text))
    assert paths
    assert not [path for path in sorted(paths) if not (root / path).is_file()]
