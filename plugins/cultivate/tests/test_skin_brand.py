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
    host = (REPO / "frontend/src/styles.css").read_text()
    plugin = (ROOT / "frontend/src/style.css").read_text()

    def declaration(css: str, selector: str, property_name: str) -> str:
        rule = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]+)\}", css)
        assert rule is not None, selector
        value = re.search(r"(?:^|;)\s*" + re.escape(property_name) + r":\s*([^;]+)", rule[1])
        assert value is not None, (selector, property_name)
        return value[1].strip()

    def resolved(value: str) -> str:
        variable = re.fullmatch(r"var\((--mimir-[\w-]+)(?:,\s*[^)]+)?\)", value)
        assert variable is not None, value
        return tokens[variable[1]]

    # The host's ordinary button starts with white text, but its :active fill
    # uses the skin's purple. Verify the scoped override, not an imagined pair.
    assert resolved(declaration(host, ".ui-button", "color")) == color("text")
    pressed = resolved(declaration(host, ".ui-button:active", "background"))
    pressed_text = resolved(declaration(
        plugin, '.skin-root[data-skin="squashmerge"] .ui-button:active:not(:disabled)', "color",
    ))
    assert pressed_text == color("background")
    for foreground, background in (
        (color("chrome-accent-text"), color("chrome-accent")),
        (pressed_text, pressed),
        (color("background"), tokens["--mimir-interaction-active-background"]),
        (color("focus-ring"), color("background")),
        (color("focus-ring"), color("panel-background")),
        (color("text"), color("panel-background")),
        (color("text-muted"), color("panel-background")),
        (color("code-text"), color("code-background")),
        *((color(f"status-{state}"), color(f"status-{state}-background"))
          for state in ("info", "success", "warning", "danger")),
    ):
        assert _contrast(foreground, background) >= 4.5
    assert declaration(plugin, ".cultivate a", "color") == "var(--cultivate-link)"
    link = resolved(declaration(plugin, ".cultivate", "--cultivate-link"))
    assert link == color("focus-ring")
    assert "text-decoration: underline" in plugin
    for background in (color("background"), color("panel-background")):
        assert _contrast(link, background) >= 4.5
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
    assert 'alt="Cultivate, a Mimir plugin by SquashMerge"' in source
    assert re.search(r'!selected\s*\?\s*<div className="cultivate-identity"', source)
    assert 'data-testid="cultivate-mobile-name">Cultivate</span>' in source
    css = (ROOT / "frontend/src/style.css").read_text()
    assert "width: min(100%, 400px)" in css
    assert "min-height: 44px" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "outline-offset: 3px" in css
    assert not re.search(r"@import|https?://|fonts\.googleapis", css, re.I)


def test_emitted_overview_has_wide_artwork_and_mobile_text() -> None:
    css = (ROOT / "mimir_cultivate/frontend/cultivate.v1.css").read_text()
    js = (ROOT / "mimir_cultivate/frontend/cultivate.v1.js").read_text()
    assert "Cultivate, a Mimir plugin by SquashMerge" in js
    assert "cultivate-overview-logo" in js
    assert "cultivate-mobile-name" in js
    assert re.search(r"\.cultivate-logo\{[^}]*display:block", css)
    assert ".cultivate-mobile-name{display:none" in css
    mobile = css.split("@media (width<=420px)", 1)
    assert len(mobile) == 2
    assert ".cultivate-logo{display:none}" in mobile[1]
    assert re.search(r"\.cultivate-mobile-name\{[^}]*display:block", mobile[1])
    assert '.skin-root[data-skin=squashmerge] .ui-button:active:not(:disabled){color:var(--mimir-color-background)}' in css
