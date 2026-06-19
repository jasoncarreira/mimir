#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["lottie==0.7.2"]
# ///
"""Generate the default retro agent-character Lottie set (chainlink #565).

Builds a simple, cohesive CRT-terminal-face character with one animation per
agent state (idle / thinking / typing / tool / error) and writes each as a
dotLottie (.lottie) file the frontend's <dotlottie-wc> consumes.

Aesthetic: phosphor-green CRT monitor (github #726 retro/Winamp vibe). The art
is deliberately geometric — programmatic vector shapes, not illustrated mascot
art — so it ships as a real animated default that a designer can later replace
without any code change (just swap the .lottie files).

Run:  uv run --script scripts/generate_agent_character.py
      (the only dep, python-lottie, is declared inline via PEP 723 above, so
      this works from a clean checkout with no project install or sync.)
Out:  frontend/src/assets/agent-character/{idle,thinking,typing,tool,error}.lottie
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

from lottie import Color, Point, objects
from lottie.exporters.core import export_lottie

W = H = 256
FR = 30  # fps

# Phosphor palette.
SCREEN = Color(0.04, 0.07, 0.05)      # near-black CRT glass
BEZEL = Color(0.10, 0.13, 0.11)
GREEN = Color(0.24, 0.95, 0.48)       # phosphor green
GREEN_DIM = Color(0.14, 0.55, 0.30)
AMBER = Color(1.00, 0.75, 0.20)
RED = Color(1.00, 0.30, 0.30)

OUT = Path("frontend/src/assets/agent-character")


def _rrect(layer: objects.ShapeLayer, cx, cy, w, h, color, radius=14.0):
    grp = layer.add_shape(objects.Group())
    rect = grp.add_shape(objects.Rect())
    rect.position.value = Point(cx, cy)
    rect.size.value = Point(w, h)
    rect.rounded.value = radius
    grp.add_shape(objects.Fill(color))
    return grp


def _ellipse(layer, cx, cy, w, h, color):
    grp = layer.add_shape(objects.Group())
    el = grp.add_shape(objects.Ellipse())
    el.position.value = Point(cx, cy)
    el.size.value = Point(w, h)
    grp.add_shape(objects.Fill(color))
    return grp, el


def _base(name: str) -> tuple[objects.Animation, objects.ShapeLayer]:
    """New animation + the frontmost face layer.

    python-lottie renders ``layers[0]`` ON TOP, so layers must be added
    front-to-back. The face layer is added first (frontmost); the CRT
    bezel/screen backplate is added LAST via _backplate() so the dark glass
    sits behind the eyes/mouth instead of covering them.
    """
    an = objects.Animation(60)
    an.width, an.height, an.frame_rate = W, H, FR
    an.name = name
    face = an.add_layer(objects.ShapeLayer())
    return an, face


def _backplate(an: objects.Animation) -> None:
    """CRT bezel + glass screen as the backmost layer — call LAST."""
    back = an.add_layer(objects.ShapeLayer())
    # First shape is on top within a layer: the glass sits inset on the bezel.
    _rrect(back, 128, 122, 188, 156, SCREEN, radius=18)
    _rrect(back, 128, 128, 220, 200, BEZEL, radius=26)


def _scanline(an: objects.Animation, color=GREEN_DIM):
    """A faint horizontal scanline sweeping down the glass — pure retro."""
    layer = an.add_layer(objects.ShapeLayer())
    grp = _rrect(layer, 128, 60, 180, 4, color, radius=2)
    tf = grp.transform
    tf.opacity.add_keyframe(0, 22)
    tf.opacity.add_keyframe(30, 10)
    tf.opacity.add_keyframe(60, 22)
    tf.position.add_keyframe(0, Point(0, 0))
    tf.position.add_keyframe(60, Point(0, 130))


def _eyes(face, *, y=110, open_h=34, color=GREEN):
    left, lel = _ellipse(face, 92, y, 30, open_h, color)
    right, rel = _ellipse(face, 164, y, 30, open_h, color)
    return (left, lel), (right, rel)


def build_idle() -> objects.Animation:
    an, face = _base("idle")
    _scanline(an)
    (lg, _le), (rg, _re) = _eyes(face)
    # Slow blink near the end of the loop + gentle bob.
    for grp in (lg, rg):
        sc = grp.transform.scale
        sc.add_keyframe(0, Point(100, 100))
        sc.add_keyframe(48, Point(100, 100))
        sc.add_keyframe(52, Point(100, 8))
        sc.add_keyframe(56, Point(100, 100))
    mouth = _rrect(face, 128, 150, 56, 8, GREEN_DIM, radius=4)
    bob = face.transform.position
    bob.add_keyframe(0, Point(0, 0))
    bob.add_keyframe(30, Point(0, -4))
    bob.add_keyframe(60, Point(0, 0))
    return an


def build_thinking() -> objects.Animation:
    an, face = _base("thinking")
    _scanline(an, AMBER)
    (lg, _l), (rg, _r) = _eyes(face, y=104)
    # Eyes glance up-left, pondering.
    for grp in (lg, rg):
        p = grp.transform.position
        p.add_keyframe(0, Point(0, 0))
        p.add_keyframe(30, Point(-7, -6))
        p.add_keyframe(60, Point(0, 0))
    # Three thinking dots that pulse in sequence.
    for i, x in enumerate((104, 128, 152)):
        dg, _d = _ellipse(face, x, 156, 12, 12, AMBER)
        op = dg.transform.opacity
        base = i * 12
        op.add_keyframe(0, 20)
        op.add_keyframe((base + 8) % 60, 100)
        op.add_keyframe((base + 24) % 60, 20)
        op.add_keyframe(60, 20)
    return an


def build_typing() -> objects.Animation:
    an, face = _base("typing")
    _scanline(an)
    _eyes(face, y=104, open_h=30)
    # A blinking block cursor.
    cur = _rrect(face, 110, 156, 16, 22, GREEN, radius=2)
    op = cur.transform.opacity
    for f in (0, 15, 30, 45, 60):
        op.add_keyframe(f, 100 if (f // 15) % 2 == 0 else 0)
    # Little equalizer bars bouncing, Winamp-style.
    for i, x in enumerate((132, 148, 164)):
        bg = _rrect(face, x, 156, 8, 24, GREEN_DIM, radius=2)
        sc = bg.transform.scale
        phase = i * 10
        sc.add_keyframe(0, Point(100, 40))
        sc.add_keyframe((phase + 15) % 60, Point(100, 100))
        sc.add_keyframe((phase + 30) % 60, Point(100, 40))
        sc.add_keyframe(60, Point(100, 40))
    return an


def build_tool() -> objects.Animation:
    an, face = _base("tool")
    _scanline(an)
    _eyes(face, y=104, open_h=26)
    # A spinning ring (stroked ellipse with a gap) = "working".
    grp = face.add_shape(objects.Group())
    el = grp.add_shape(objects.Ellipse())
    el.position.value = Point(128, 158)
    el.size.value = Point(46, 46)
    stroke = grp.add_shape(objects.Stroke(GREEN, 7))
    trim = grp.add_shape(objects.Trim())
    trim.start.value = 0
    trim.end.value = 70
    grp.transform.position.value = Point(128, 158)
    grp.transform.anchor_point.value = Point(128, 158)
    rot = grp.transform.rotation
    rot.add_keyframe(0, 0)
    rot.add_keyframe(60, 360)
    del stroke
    return an


def build_error() -> objects.Animation:
    an, face = _base("error")
    _scanline(an, RED)
    # Alarmed red eyes.
    _eyes(face, y=108, open_h=20, color=RED)
    # Flat alarmed mouth + a blinking "!" via a bar + dot.
    _rrect(face, 128, 152, 50, 8, RED, radius=4)
    # Screen shake: jitter the whole face horizontally.
    p = face.transform.position
    for f, dx in [(0, 0), (6, -6), (12, 6), (18, -5), (24, 5), (30, -3), (36, 3), (42, 0), (60, 0)]:
        p.add_keyframe(f, Point(dx, 0))
    return an


def write_dotlottie(an: objects.Animation, path: Path) -> None:
    """Package one animation as a dotLottie (.lottie) ZIP: manifest + the JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json")
    export_lottie(an, str(tmp))
    anim_json = tmp.read_text(encoding="utf-8")
    tmp.unlink()
    manifest = {
        "version": "1.0.0",
        "generator": "mimir/scripts/generate_agent_character.py",
        "animations": [{"id": an.name, "loop": True, "autoplay": True, "speed": 1.0}],
    }
    # Deterministic ZIP: fixed timestamps + mode so regenerating from a clean
    # checkout reproduces byte-identical .lottie files (no spurious diffs).
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _zip_write(zf, "manifest.json", json.dumps(manifest, separators=(",", ":")))
        _zip_write(zf, f"animations/{an.name}.json", anim_json)


def _zip_write(zf: zipfile.ZipFile, name: str, data: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def main() -> None:
    builders = {
        "idle": build_idle,
        "thinking": build_thinking,
        "typing": build_typing,
        "tool": build_tool,
        "error": build_error,
    }
    for state, builder in builders.items():
        out = OUT / f"{state}.lottie"
        an = builder()
        _backplate(an)  # CRT bezel/screen behind everything (backmost layer)
        write_dotlottie(an, out)
        print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()


_ = math  # reserved for future curved motion paths
