"""Static conformance checks for the React dashboard skin contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
STYLES_PATH = ROOT / "frontend/src/styles.css"
SKINS_PATH = ROOT / "frontend/src/skins"
PROVIDER_PATH = SKINS_PATH / "SkinProvider.tsx"


def _token_mapping() -> dict[str, str]:
    source = PROVIDER_PATH.read_text(encoding="utf-8")
    block = re.search(
        r"const tokenCssVariableNames = \{(?P<body>.*?)\n\}", source, re.S
    )
    assert block is not None
    return dict(
        re.findall(
            r'(\w+):\s*(?:\n\s*)?"(--mimir-[^"]+)"', block.group("body")
        )
    )


def _registered_skin_tokens() -> dict[str, dict[str, str]]:
    provider = PROVIDER_PATH.read_text(encoding="utf-8")
    registry = re.search(
        r"export const localSkins = \{(?P<body>.*?)\n\}", provider, re.S
    )
    assert registry is not None
    imports = dict(
        re.findall(r'import \{ (\w+) \} from "\./([^"]+)";', provider)
    )
    registered = re.findall(r'"[^"]+":\s*(\w+)', registry.group("body"))

    skins: dict[str, dict[str, str]] = {}
    for variable in registered:
        source = (SKINS_PATH / f"{imports[variable]}.ts").read_text(encoding="utf-8")
        skin_id = re.search(r'\bid:\s*"([^"]+)"', source)
        token_block = re.search(
            r"\btokens:\s*\{(?P<body>.*?)\n\s{2}\},\n\s{2}chrome:", source, re.S
        )
        assert skin_id is not None and token_block is not None
        skins[skin_id.group(1)] = {
            key: value
            for key, quote, value in re.findall(
                r"^ {4}(\w+):\s*(['\"])(.*?)\2",
                token_block.group("body"),
                re.M,
            )
        }
    return skins


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _mix_srgb(foreground: str, background: str, weight: float) -> str:
    foreground_channels = [int(foreground[index : index + 2], 16) for index in (1, 3, 5)]
    background_channels = [int(background[index : index + 2], 16) for index in (1, 3, 5)]
    channels = [
        round(foreground_channel * weight + background_channel * (1 - weight))
        for foreground_channel, background_channel in zip(
            foreground_channels, background_channels, strict=True
        )
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def test_every_referenced_skin_variable_is_defined_by_every_registered_skin():
    styles = STYLES_PATH.read_text(encoding="utf-8")
    referenced = set(re.findall(r"var\((--mimir-[a-z0-9-]+)", styles))
    mapping = _token_mapping()
    variables_to_tokens = {variable: token for token, variable in mapping.items()}

    assert referenced <= variables_to_tokens.keys()
    for skin_id, tokens in _registered_skin_tokens().items():
        missing = {
            variable
            for variable in referenced
            if variables_to_tokens[variable] not in tokens
        }
        assert not missing, f"{skin_id} does not define: {sorted(missing)}"


def test_affected_surfaces_use_skin_tokens_and_real_trigger_names():
    styles = STYLES_PATH.read_text(encoding="utf-8")

    assert "--mimir-color-danger" not in styles
    assert "--mimir-color-page-background" not in styles
    assert "--mimir-font-size-2xl" not in styles
    assert "color: var(--mimir-color-status-danger, #8b3a3a);" in styles
    assert "background: var(--mimir-color-panel-background-muted, #f1f5f2);" in styles
    assert "font-size: var(--mimir-font-size-lg, 1.125rem);" in styles

    hardcoded_neon_colors = {
        "#5be88f",
        "#5be8c0",
        "#7fb6ff",
        "#c9a3ff",
        "#e7b357",
        "#ff8fb0",
        "#a6a6ff",
        "#8fd6e6",
        "#ff8d8d",
    }
    assert not hardcoded_neon_colors.intersection(re.findall(r"#[0-9a-fA-F]+", styles))
    hardcoded_neon_channels = {
        "47 224 160",
        "90 150 255",
        "224 178 74",
        "160 90 255",
        "47 224 112",
        "255 90 130",
        "130 130 255",
        "90 190 215",
    }
    assert not hardcoded_neon_channels.intersection(
        re.findall(r"rgb\((\d+ \d+ \d+)", styles)
    )

    for token in (
        "timeline-tool-result",
        "timeline-tool-call",
        "timeline-reasoning",
        "chrome-accent",
        "status-info",
        "status-danger",
    ):
        assert f"var(--mimir-color-{token}" in styles

    trigger_tokens = {
        "saga_session_end": "saga-session-end",
        "scheduled_tick": "scheduled-tick",
        "poller": "poller",
        "user_message": "user-message",
        "synthesis": "synthesis",
        "heartbeat": "heartbeat",
        "claude_code_spawn": "claude-code-spawn",
        "shell_job_complete": "shell-job-complete",
    }
    for trigger, token in trigger_tokens.items():
        assert f'[data-trigger="{trigger}"]' in styles
        assert f"var(--mimir-color-trigger-{token}" in styles
    assert '[data-trigger="spawn"]' not in styles
    assert '[data-trigger="job"]' not in styles
    assert "color: var(--turn-trigger-color);" in styles
    assert "background: color-mix(in srgb, var(--turn-trigger-color) 14%" in styles
    assert "border-color: var(--turn-trigger-color);" in styles


def test_saga_kinds_validation_and_user_trigger_meet_contrast_per_skin():
    kind_tokens = (
        "colorTimelineToolResult",
        "colorTimelineReasoning",
        "colorChromeAccent",
    )
    outcomes: dict[str, dict[str, float]] = {}

    for skin_id, tokens in _registered_skin_tokens().items():
        panel = tokens["colorPanelBackground"]
        trigger = tokens["colorTriggerUserMessage"]
        trigger_background = _mix_srgb(
            trigger, tokens["colorPanelBackgroundMuted"], 0.14
        )
        outcomes[skin_id] = {
            "weakest saga kind": min(
                _contrast_ratio(tokens[token], panel) for token in kind_tokens
            ),
            "validation error": _contrast_ratio(tokens["colorStatusDanger"], panel),
            "user trigger": _contrast_ratio(trigger, trigger_background),
        }

    assert set(outcomes) == {"default-retro", "neon-terminal", "cosmic-nebula"}
    for skin_id, surfaces in outcomes.items():
        for surface, ratio in surfaces.items():
            assert ratio >= 4.5, f"{skin_id} {surface} contrast is only {ratio:.2f}:1"
