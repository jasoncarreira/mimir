"""SAGA-payload rendering helpers.

Used by ``mimir.agent`` (pre-message memory injection) and
``mimir.tools.memory`` (the langchain memory_query tool). Renders
the ``{atoms, triples, observations, raws}`` shape that
``SagaClient.query`` returns into the prompt block the agent sees.

Pure functions — no dependencies, no state.
"""
from __future__ import annotations

from typing import Any


def _atom_label(atom: dict[str, Any]) -> str:
    """Pick the most descriptive tag for an atom in the rendered prompt."""
    mt = atom.get("memory_type")
    tier = atom.get("confidence_tier") or atom.get("_confidence_tier")
    base = "observation" if mt == "observation" else (atom.get("stream") or atom.get("kind") or mt or "atom")
    if tier and tier != "none":
        return f"{base}/{tier}"
    return base


#: Per-atom content cap when rendering hits — bumped 240→1200 (2026-05-14)
#: after LongMemEval surfaced answers buried at chars 200-400 of haystack
#: turn transcripts. See PR #166 for the full diagnostic.
_ATOM_CONTENT_CAP = 1200


def _format_atoms(hits: list[dict[str, Any]]) -> str:
    """Render SAGA hits as a brief bullet list — tag + content, no IDs."""
    if not hits:
        return "(no atoms)"
    lines: list[str] = []
    for h in hits:
        label = _atom_label(h)
        score = h.get("score") or h.get("similarity")
        content = (h.get("content") or "").strip().replace("\n", " ")
        if len(content) > _ATOM_CONTENT_CAP:
            content = content[:_ATOM_CONTENT_CAP] + "…"
        score_str = f" ({score:.3f})" if isinstance(score, (int, float)) else ""
        lines.append(f"- [{label}{score_str}] {content}")
    return "\n".join(lines)


def _triples_in_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for t in payload.get("triples") or []:
        if isinstance(t, dict):
            out.append(t)
    return out


def _source_atom_ids_from_triples(payload: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in _triples_in_payload(payload):
        atom_id = t.get("source_atom_id")
        if isinstance(atom_id, str) and atom_id and atom_id not in seen:
            out.append(atom_id)
            seen.add(atom_id)
    return out


def _fmt_iso_date(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return raw[:10]


def _format_triples(triples: list[dict[str, Any]]) -> str:
    if not triples:
        return ""
    lines: list[str] = []
    for t in triples:
        subj = t.get("subject") or "?"
        pred = t.get("predicate") or "?"
        obj = t.get("object") or "?"
        valid_from = _fmt_iso_date(t.get("valid_from"))
        valid_until = _fmt_iso_date(t.get("valid_until"))
        if valid_from and valid_until:
            date_part = f" [valid {valid_from} → {valid_until}]"
        elif valid_from:
            date_part = f" [valid {valid_from} → present]"
        elif valid_until:
            date_part = f" [valid → {valid_until}]"
        else:
            date_part = ""
        conf = t.get("confidence")
        conf_part = ""
        if isinstance(conf, (int, float)) and conf < 1.0:
            conf_part = f" (conf {conf:.2f})"
        lines.append(f"- ({subj}, {pred}, {obj}){date_part}{conf_part}")
    return "\n".join(lines)


def _atoms_in_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for key in ("observations", "raws", "atoms", "_raw_atoms", "raw_atoms"):
        for atom in payload.get(key) or []:
            if isinstance(atom, dict):
                out.append(atom)
    sections = payload.get("sections") or {}
    if isinstance(sections, dict):
        for atoms in sections.values():
            for a in atoms or []:
                if isinstance(a, dict):
                    out.append(a)
    return out


def _format_saga_payload(payload: dict[str, Any]) -> str:
    atoms = _atoms_in_payload(payload)
    triples = _triples_in_payload(payload)
    parts: list[str] = []
    if atoms:
        parts.append(_format_atoms(atoms))
    if triples:
        triples_block = _format_triples(triples)
        if triples_block:
            if parts:
                parts.append("")
            parts.append("Triples:")
            parts.append(triples_block)
    if not parts:
        return "(no atoms)"
    return "\n".join(parts)


def _atom_ids_from_response(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for atom in _atoms_in_payload(payload):
        aid = atom.get("id") or atom.get("atom_id")
        if aid:
            out.append(str(aid))
    return out


def _hits_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for atom in _atoms_in_payload(payload):
        item = {
            "atom_id": atom.get("id") or atom.get("atom_id"),
            "stream": atom.get("stream") or atom.get("kind"),
            "content": atom.get("content"),
            "score": atom.get("_activation") or atom.get("score") or atom.get("similarity"),
            "confidence": atom.get("encoding_confidence") or atom.get("confidence"),
        }
        mt = atom.get("memory_type")
        if mt:
            item["memory_type"] = mt
        tier = atom.get("confidence_tier") or atom.get("_confidence_tier")
        if tier:
            item["confidence_tier"] = tier
        ec = atom.get("evidence_count")
        if ec is not None:
            item["evidence_count"] = ec
        out.append(item)
    return out


# ────────────────────────────────────────────────────────────────────
# Stubs for compatibility with code that hasn't been updated yet.
# ────────────────────────────────────────────────────────────────────


