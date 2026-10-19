"""Cultivate's packaged skin and artwork follow the SquashMerge dark contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from mimir.plugins.manifest import _SKIN_TOKENS, parse_manifest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
SKIN = ROOT / "mimir_cultivate/skins/squashmerge.json"
LOGO = ROOT / "mimir_cultivate/brand/cultivate-logo-dark-v1.png"


def _contrast(foreground: str, background: str) -> float:
    def luminance(hex_color: str) -> float:
        channels = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_skin_is_complete_and_selected_by_manifest() -> None:
    raw = (ROOT / "mimir-plugin.toml").read_bytes()
    package_root = ROOT / "mimir_cultivate"
    manifest = parse_manifest(
        raw, entry_name="cultivate", resource_root=package_root,
        distribution_files=frozenset(path for path in package_root.rglob("*") if path.is_file()),
    )
    skin = json.loads(SKIN.read_text())
    assert manifest.themes == ("skins/squashmerge.json",)
    assert manifest.skin_defaults == ("squashmerge",)
    assert skin["tokens"].keys() == _SKIN_TOKENS
    assert skin["characterRenderer"]["assets"] == []
    assert skin.get("fonts", []) == []


def test_dark_skin_flat_text_and_control_contrast() -> None:
    tokens = json.loads(SKIN.read_text())["tokens"]
    color = lambda name: tokens[f"--mimir-color-{name}"]
    for foreground, background in (
        (color("chrome-accent-text"), color("chrome-accent")),
        (color("background"), tokens["--mimir-interaction-active-background"]),
        (color("focus-ring"), color("background")),
        (color("text"), color("panel-background")),
        (color("text-muted"), color("panel-background")),
        (color("code-text"), color("code-background")),
    ):
        assert _contrast(foreground, background) >= 4.5
    for background in (color("background"), color("panel-background")):
        assert _contrast(color("panel-border"), background) >= 3


def test_artwork_and_frontend_use_local_resources_only() -> None:
    assert hashlib.sha256(LOGO.read_bytes()).digest() == hashlib.sha256(
        (REPO / "brand/logos/cultivate-logo-dark-v1.png").read_bytes()
    ).digest()
    skin = json.loads(SKIN.read_text())
    assert skin["assets"] == ["brand/cultivate-logo-dark-v1.png"]
    source = (ROOT / "frontend/src/runs.tsx").read_text()
    assert 'data-testid="cultivate-overview-logo"' in source
    assert 'alt=""' in source
    assert re.search(r'!selected\s*\?\s*<img', source)
    css = (ROOT / "frontend/src/style.css").read_text()
    assert "width: min(100%, 400px)" in css
    assert "min-height: 44px" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "outline-offset: 3px" in css
    assert not re.search(r"@import|https?://|fonts\.googleapis", css, re.I)
