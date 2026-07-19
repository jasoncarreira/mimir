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


def _provenance_tag(item: dict[str, Any]) -> str:
    """Render only immutable server-stamped provenance fields."""
    fields: list[str] = []
    for label, key in (
        ("trigger", "origin_trigger"),
        ("ref", "origin_ref"),
        ("captured", "captured_at"),
    ):
        value = item.get(key)
        if isinstance(value, str) and value:
            fields.append(f"{label}={value.strip().replace(chr(10), ' ')}")
    return f" [{'; '.join(fields)}]" if fields else ""


def _trust_groups(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trusted = [item for item in items if item.get("integrity") == "trusted"]
    # Missing/invalid legacy provenance fails closed and is visibly untrusted.
    untrusted = [item for item in items if item.get("integrity") != "trusted"]
    return trusted, untrusted


#: Per-atom content cap when rendering hits into the pre-message hook
#: prompt block. Was 240 (tuned for short conversational atoms) — bumped
#: to 1200 on 2026-05-14 after LongMemEval bench surfaced answers buried
#: at chars 200-400 of multi-sentence turn transcripts (e.g. "I created a
#: Spotify playlist called Summer Vibes" sits at char ~250 of a single
#: user turn that opens with a different question). The old cap cut
#: those off mid-answer; the agent saw "…called Summer Vib…" and replied
#: "I don't have that information."
#:
#: Cost: top_k=12 × 1200 chars × ~3.5 chars/token ≈ 4k input tokens
#: per pre-message hook fire. Was 1k under the 240 cap. The 3k delta is
#: a rounding error against the 100k+ context windows in flight today.
#:
#: Override at construction time / future config-key if you need to
#: tune per-deployment.
_ATOM_CONTENT_CAP = 1200


def _format_atoms(hits: list[dict[str, Any]]) -> str:
    """Render SAGA hits grouped by their server-stamped origin trust."""
    if not hits:
        return "(no atoms)"
    lines: list[str] = []
    trusted, untrusted = _trust_groups(hits)
    for heading, group in (("Trusted-origin memories:", trusted), ("Untrusted-origin memories:", untrusted)):
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append(heading)
        for h in group:
            label = _atom_label(h)
            score = h.get("score") or h.get("similarity")
            content = (h.get("content") or "").strip().replace("\n", " ")
            if len(content) > _ATOM_CONTENT_CAP:
                content = content[:_ATOM_CONTENT_CAP] + "…"
            score_str = f" ({score:.3f})" if isinstance(score, (int, float)) else ""
            lines.append(f"- [{label}{score_str}]{_provenance_tag(h)} {content}")
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
    trusted, untrusted = _trust_groups(triples)
    for heading, group in (("Trusted-origin triples:", trusted), ("Untrusted-origin triples:", untrusted)):
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append(heading)
        for t in group:
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
            lines.append(
                f"- ({subj}, {pred}, {obj}){date_part}{conf_part}{_provenance_tag(t)}"
            )
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
