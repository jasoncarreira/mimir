"""ASI-rich metrics for SAGA cluster→observation prompt candidates.

The evaluator is deliberately mixed-mode:

* parser compatibility is a hard gate, using the same parsers production SAGA
  uses for rich consolidation output;
* symbolic checks preserve identifiers/dates/numbers/names that embeddings often
  blur away;
* support checks flag introduced identifiers/dates/numbers/names not grounded in
  any source atom, with source/candidate spans in ASI;
* retrieval geometry is optional and dependency-injected so tests and offline
  pilots can use deterministic vectors while live pilots can plug in a real
  embedding provider.

It is not a full entailment judge. The support check is a conservative heuristic
for high-signal unsupported artifacts; an LLM judge can be layered in later
without changing the GEPA adapter contract.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Mapping, Sequence

from mimir.saga.synthesize import _parse_contradictions, _parse_observation
from mimir.saga.triples import parse_triples

EmbeddingFn = Callable[[str], Sequence[float]]

_HEADER_RE = re.compile(r"^\s*(OBSERVATION|TRIPLES|CONTRADICTIONS)\s*:\s*$", re.I | re.M)
_ID_RE = re.compile(
    r"\b(?:PR|Chainlink|issue)\s*#\s*[A-Za-z0-9][A-Za-z0-9._:-]*\b"
    r"|\bRFC\s*\d+[A-Za-z0-9._:-]*\b"
    r"|\barXiv:\s*\d{4}\.\d{4,5}\b"
    r"|\b[a-f0-9]{12,16}\b"
    r"|\b\d{4}\.\d{4,5}\b"
    r"|(?<![A-Za-z0-9])/[A-Za-z0-9._~:/#-]+"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?(?:\s*(?:dimensions?|d|%|pp))?(?![A-Za-z0-9])",
    re.I,
)
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[- ][A-Z][A-Za-z0-9]*){0,4}\b")
_GENERIC_NAMES = {
    "A", "An", "And", "Atom", "Atoms", "Candidate", "Contradictions",
    "Current", "Do", "If", "It", "None", "Observation", "Output", "Rules",
    "The", "They", "This", "Triples", "User", "You",
}


_PROMPT_ARXIV_RE = re.compile(r"\barXiv:\s*\d{4}\.\d{4,5}\b")
_PROMPT_ISSUE_RE = re.compile(
    r"\b(?:PR|Chainlink|issue)\s*#\s*[A-Za-z0-9][A-Za-z0-9._:-]*\b", re.I
)
_PROMPT_PATH_RE = re.compile(
    r"(?<!\w)(?:state/wiki|memory|/wiki|/use|/mimir-home|/workspace)/[A-Za-z0-9._~:/#-]+"
)
_PROMPT_OVERFIT_SECTION_MARKERS = (
    "known compact aliases",
    "known arxiv ids",
    "known entity names",
    "domain-specific guidance",
    "concepts that may need compact triple objects",
)
_FROZEN_EXAMPLE_BLOCK_RE = re.compile(
    r"^\s*(?:BEGIN|START) FROZEN EXAMPLE\s*$.*?^\s*(?:END|STOP) FROZEN EXAMPLE\s*$",
    re.I | re.M | re.S,
)
_META_CLUSTER_WRAPPER_RE = re.compile(
    r"\b(?:these|this|the|in these|in this)\s+"
    r"(?:\d+\s+)?(?:source\s+)?(?:atoms?|cluster|clustered atoms?|atom set)\s+"
    r"(?:document|documents|indicate|indicates|show|shows|suggest|suggests|record|records|describe|describes)\b"
    r"|\b(?:this|the)\s+cluster\s+(?:is about|concerns|captures)\b"
    r"|\bthe\s+atoms\s+(?:are about|concern|capture)\b",
    re.I,
)


def score_prompt_candidate(prompt: str) -> dict[str, Any]:
    """Return candidate-prompt over-specification signals and a score penalty.

    This regularizes GEPA candidates at the *prompt text* level. Generated
    observations should preserve atom-specific IDs and paths when the atoms
    contain them; the prompt itself should not arrive preloaded with a pilot
    corpus glossary, hard-coded paper IDs, path aliases, or long entity lists.
    """

    text = prompt or ""
    production_text, frozen_blocks = _strip_frozen_example_blocks(text)
    lower = production_text.lower()
    char_count = len(text)
    production_char_count = len(production_text)
    arxiv_ids = _dedupe(
        m.group(0).replace(" ", "") for m in _PROMPT_ARXIV_RE.finditer(production_text)
    )
    issue_ids = _dedupe(m.group(0) for m in _PROMPT_ISSUE_RE.finditer(production_text))
    path_literals = _dedupe(m.group(0) for m in _PROMPT_PATH_RE.finditer(production_text))
    marker_hits = [marker for marker in _PROMPT_OVERFIT_SECTION_MARKERS if marker in lower]
    bullet_entity_lines = _prompt_entity_bullet_lines(production_text)

    soft_limit = 5_000
    hard_limit = 9_000
    length_penalty = 0.0
    if char_count > soft_limit:
        length_penalty = min(
            0.25, 0.25 * (char_count - soft_limit) / (hard_limit - soft_limit)
        )

    literal_penalty = min(0.25, 0.035 * max(0, len(arxiv_ids) - 2) + 0.03 * len(issue_ids))
    path_penalty = min(0.15, 0.025 * max(0, len(path_literals) - 2))
    section_penalty = min(0.20, 0.08 * len(marker_hits))
    entity_list_penalty = min(0.15, 0.01 * max(0, len(bullet_entity_lines) - 8))
    penalty = min(
        0.60,
        length_penalty
        + literal_penalty
        + path_penalty
        + section_penalty
        + entity_list_penalty,
    )

    hard_fail_reasons: list[str] = []
    if "{indexed_atoms}" not in text:
        hard_fail_reasons.append("missing_indexed_atoms_placeholder")
    if arxiv_ids:
        hard_fail_reasons.append("hardcoded_arxiv_ids")
    if issue_ids:
        hard_fail_reasons.append("hardcoded_pr_or_issue_ids")
    if path_literals:
        hard_fail_reasons.append("hardcoded_paths")
    if marker_hits:
        hard_fail_reasons.append("corpus_glossary_section")
    if len(bullet_entity_lines) > 8:
        hard_fail_reasons.append("large_entity_glossary")

    signals = []
    if "{indexed_atoms}" not in text:
        signals.append("missing_indexed_atoms_placeholder")
    if length_penalty:
        signals.append("too_long")
    if literal_penalty:
        signals.append("hardcoded_identifiers")
    if path_penalty:
        signals.append("hardcoded_paths")
    if section_penalty:
        signals.append("corpus_glossary_section")
    if entity_list_penalty:
        signals.append("large_entity_list")

    return {
        "char_count": char_count,
        "production_char_count": production_char_count,
        "frozen_example_blocks": len(frozen_blocks),
        "pass": not hard_fail_reasons,
        "gate": {
            "passed": not hard_fail_reasons,
            "hard_fail_reasons": hard_fail_reasons,
        },
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "penalty": penalty,
        "signals": signals,
        "counts": {
            "arxiv_ids": len(arxiv_ids),
            "issue_ids": len(issue_ids),
            "path_literals": len(path_literals),
            "glossary_markers": len(marker_hits),
            "entity_bullet_lines": len(bullet_entity_lines),
        },
        "examples": {
            "arxiv_ids": arxiv_ids[:8],
            "issue_ids": issue_ids[:8],
            "path_literals": path_literals[:8],
            "glossary_markers": marker_hits,
            "entity_bullet_lines": bullet_entity_lines[:8],
        },
    }


def _strip_frozen_example_blocks(text: str) -> tuple[str, list[str]]:
    """Remove deliberately frozen examples before prompt-overfit literal checks.

    A candidate may include literal arXiv IDs, paths, or PR numbers inside a
    frozen example block, but that block must be explicitly delimited so a
    production prompt builder can strip it before deployment. Undelimited
    corpus literals remain a prompt-overfit gate failure.
    """

    frozen_blocks = [m.group(0) for m in _FROZEN_EXAMPLE_BLOCK_RE.finditer(text)]
    production_text = _FROZEN_EXAMPLE_BLOCK_RE.sub("", text)
    return production_text, frozen_blocks


def _prompt_entity_bullet_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        value = stripped[1:].strip().strip("`")
        if not value or len(value) > 80:
            continue
        if re.fullmatch(r"[A-Z][A-Za-z0-9]*(?:[-_ ][A-Z0-9][A-Za-z0-9]*){0,4}", value):
            out.append(value)
        elif re.fullmatch(r"arXiv:\s*\d{4}\.\d{4,5}", value):
            out.append(value)
    return _dedupe(out)


@dataclass
class EvaluationResult:
    """Scalar + Actionable Side Information for one candidate/example pair."""

    score: float
    asi: dict[str, Any]


def score_candidate(
    example: Mapping[str, Any],
    candidate_output: str | Mapping[str, Any],
    *,
    embedding_fn: EmbeddingFn | None = None,
) -> EvaluationResult:
    """Score one candidate rich-synthesis output against one exported cluster.

    ``candidate_output`` may be the raw rich LLM response or a mapping carrying
    ``content``/``raw``. Raw text is preferred because parser compatibility is a
    target metric; dict support exists for tests and adapter stubs.
    """

    raw, observation = _coerce_candidate(candidate_output)
    source_atoms = _source_atoms(example)
    source_text = "\n".join(atom["content"] for atom in source_atoms)
    required = _required_details(example)

    parser = _parser_report(raw, observation)
    if not parser["ok"]:
        asi = {
            "example_id": example.get("example_id"),
            "hard_fail": "parser_compatibility",
            "parser": parser,
            "symbolic_retention": {},
            "support": {},
            "coverage": {},
            "retrieval_geometry": {},
            "score_breakdown": {"parser_gate": 0.0},
        }
        return EvaluationResult(score=0.0, asi=asi)

    retention = _symbolic_retention(observation, required)
    support = _support_report(observation, source_atoms, source_text)
    coverage = _coverage_report(observation, source_atoms)
    retrieval = _retrieval_geometry(example, observation, embedding_fn)
    concision = _concision_score(observation)
    quality = _quality_report(observation)

    hard_fail = None
    if quality["meta_cluster_wrapper"]["hits"]:
        hard_fail = "meta_cluster_wrapper"
    elif support["unsupported_high_severity"]:
        hard_fail = "unsupported_high_severity_claim"
    elif (
        _identifier_dense(example)
        and retention["required_count"] >= 3
        and retention["score"] < 0.20
        and _identifier_collapse_should_hard_fail(retention, coverage)
    ):
        hard_fail = "identifier_dense_symbolic_collapse"

    weighted = (
        0.30 * retrieval["score"]
        + 0.25 * retention["score"]
        + 0.25 * support["score"]
        + 0.15 * coverage["score"]
        + 0.05 * concision
    )
    score = 0.0 if hard_fail else max(0.0, min(1.0, weighted))

    asi = {
        "example_id": example.get("example_id"),
        "hard_fail": hard_fail,
        "parser": parser,
        "symbolic_retention": retention,
        "support": support,
        "coverage": coverage,
        "retrieval_geometry": retrieval,
        "quality": quality,
        "concision": {"score": concision, "chars": len(observation)},
        "score_breakdown": {
            "retrieval_geometry": retrieval["score"],
            "symbolic_retention": retention["score"],
            "support": support["score"],
            "coverage": coverage["score"],
            "concision": concision,
        },
    }
    return EvaluationResult(score=score, asi=asi)


def _coerce_candidate(candidate_output: str | Mapping[str, Any]) -> tuple[str, str]:
    if isinstance(candidate_output, str):
        raw = candidate_output
        observation = _parse_observation(raw)
        return raw, observation
    raw_val = candidate_output.get("raw") or candidate_output.get("text")
    if isinstance(raw_val, str):
        return raw_val, _parse_observation(raw_val)
    content = candidate_output.get("content") or candidate_output.get("observation") or ""
    observation = content if isinstance(content, str) else str(content)
    raw = f"OBSERVATION:\n{observation}\n\nTRIPLES:\nNONE\n\nCONTRADICTIONS:\nNONE\n"
    return raw, observation


def _parser_report(raw: str, observation: str) -> dict[str, Any]:
    headers = [(m.group(1).upper(), m.start()) for m in _HEADER_RE.finditer(raw or "")]
    found = [h for h, _ in headers]
    expected = ["OBSERVATION", "TRIPLES", "CONTRADICTIONS"]
    errors: list[str] = []
    if found[:3] != expected:
        errors.append(f"headers must appear in order {expected}, found {found[:3]}")
    if raw.strip().startswith("```") or raw.strip().endswith("```"):
        errors.append("markdown fence wrapper present")
    first_header = _HEADER_RE.search(raw or "")
    if first_header and raw[: first_header.start()].strip():
        errors.append("wrapper text before OBSERVATION header")
    if not observation.strip():
        errors.append("empty observation parse")
    if re.search(r"^\s*(TRIPLES|CONTRADICTIONS)\s*:", observation, re.I | re.M):
        errors.append("observation parser swallowed a later section; missing blank line")

    triples_section = _section_body(raw, "TRIPLES", next_header="CONTRADICTIONS")
    if triples_section and triples_section.strip().upper() != "NONE":
        paren_lines = [ln for ln in triples_section.splitlines() if ln.strip().startswith("(")]
        parsed = parse_triples(raw)
        if paren_lines and len(parsed) < len(paren_lines):
            errors.append("one or more TRIPLES lines were not parseable")
    return {
        "ok": not errors,
        "errors": errors,
        "headers_found": found,
        "observation": observation,
        "triples_count": len(parse_triples(raw)),
        "contradictions_count": len(_parse_contradictions(raw)),
    }


def _section_body(raw: str, header: str, *, next_header: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(header)}\s*:\s*$", re.I | re.M)
    m = pattern.search(raw or "")
    if not m:
        return ""
    body = raw[m.end():]
    end = re.search(rf"^\s*{re.escape(next_header)}\s*:\s*$", body, re.I | re.M)
    return body[: end.start()] if end else body


def _source_atoms(example: Mapping[str, Any]) -> list[dict[str, str]]:
    atoms = ((example.get("source_cluster") or {}).get("atoms") or [])
    out: list[dict[str, str]] = []
    for atom in atoms:
        if isinstance(atom, Mapping):
            content = atom.get("content")
            atom_id = atom.get("atom_id")
            if isinstance(content, str):
                out.append({"atom_id": str(atom_id or ""), "content": content})
    return out


def _required_details(example: Mapping[str, Any]) -> dict[str, list[str]]:
    ann = example.get("evaluator_annotations") or {}
    req = ann.get("required_identifiers_dates_numbers_names") or ann.get("required_details") or {}
    raw: dict[str, list[str]] = {}
    for key in ("identifiers", "dates", "numbers", "proper_names", "names"):
        vals = req.get(key) if isinstance(req, Mapping) else []
        if isinstance(vals, list):
            raw[key] = [str(v) for v in vals if isinstance(v, (str, int, float))]

    dates = _dedupe(v for v in raw.get("dates", []) if _DATE_RE.fullmatch(v))
    identifiers = _dedupe(
        ident
        for value in raw.get("identifiers", [])
        if (ident := _canonical_identifier(value)) is not None
    )
    numbers = _dedupe(
        num
        for value in raw.get("numbers", [])
        if (num := _canonical_number(value)) is not None
        and not _number_is_date_component(num, dates)
        and not _number_is_identifier_component(num, identifiers)
    )
    names = _dedupe(
        value
        for value in (raw.get("names") or raw.get("proper_names") or [])
        if value and value not in _GENERIC_NAMES
    )

    out: dict[str, list[str]] = {}
    if identifiers:
        out["identifiers"] = identifiers
    if dates:
        out["dates"] = dates
    if numbers:
        out["numbers"] = numbers
    if names:
        out["names"] = names
    return out


def _symbolic_retention(observation: str, required: Mapping[str, list[str]]) -> dict[str, Any]:
    missing: dict[str, list[str]] = {}
    mutated: dict[str, list[dict[str, str]]] = {}
    retained: dict[str, list[str]] = {}
    total = 0
    good = 0.0
    for group, vals in required.items():
        if group == "proper_names":
            continue
        retained[group] = []
        missing[group] = []
        mutated[group] = []
        for val in vals:
            if not val or val in _GENERIC_NAMES:
                continue
            total += 1
            if val in observation:
                retained[group].append(val)
                good += 1.0
                continue
            mutation = _mutation_hint(val, observation)
            if mutation:
                mutated[group].append({"expected": val, "candidate_span": mutation})
                good += 0.25
            else:
                missing[group].append(val)
    return {
        "score": 1.0 if total == 0 else good / total,
        "required_count": total,
        "retained": {k: v for k, v in retained.items() if v},
        "missing": {k: v for k, v in missing.items() if v},
        "mutated": {k: v for k, v in mutated.items() if v},
    }


def _mutation_hint(expected: str, text: str) -> str | None:
    norm_expected = _loose_norm(expected)
    if len(norm_expected) < 3:
        return None
    artifacts = _extract_supported_artifacts(text)
    candidates = set().union(*artifacts.values())
    for cand in candidates:
        if cand == expected:
            continue
        norm_cand = _loose_norm(cand)
        if _artifact_equivalent(expected, cand) or norm_cand == norm_expected:
            return cand
        if SequenceMatcher(None, norm_expected, norm_cand).ratio() >= 0.88:
            return cand
    return None


def _support_report(observation: str, source_atoms: list[dict[str, str]], source_text: str) -> dict[str, Any]:
    source_ids = _extract_supported_artifacts(source_text)
    cand_ids = _extract_supported_artifacts(observation)
    unsupported: list[dict[str, Any]] = []
    for kind, values in cand_ids.items():
        for val in values:
            if val in _GENERIC_NAMES:
                continue
            if (
                val not in source_ids.get(kind, [])
                and _mutation_hint(val, source_text) is None
                and not (kind == "numbers" and _derived_number_supported(val, source_ids.get("numbers", [])))
            ):
                unsupported.append(
                    {
                        "kind": kind,
                        "candidate_span": val,
                        "source_atom_ids_checked": [a["atom_id"] for a in source_atoms if a["atom_id"]],
                        "reason": "candidate detail not found in any source atom",
                    }
                )
    high = [u for u in unsupported if u["kind"] in {"identifiers", "dates", "numbers"}]
    penalty = min(1.0, 0.35 * len(high) + 0.15 * (len(unsupported) - len(high)))
    return {
        "score": max(0.0, 1.0 - penalty),
        "unsupported_high_severity": high,
        "unsupported_all": unsupported,
    }


def _extract_supported_artifacts(text: str) -> dict[str, list[str]]:
    raw = text or ""
    identifier_matches = [
        m for m in _ID_RE.finditer(raw)
        if _canonical_identifier(m.group(0)) is not None
    ]
    date_matches = list(_DATE_RE.finditer(raw))
    excluded = [(m.start(), m.end()) for m in identifier_matches + date_matches]
    return {
        "identifiers": sorted(
            {
                ident
                for m in identifier_matches
                if (ident := _canonical_identifier(m.group(0))) is not None
            }
        ),
        "dates": sorted({m.group(0) for m in date_matches}),
        "numbers": sorted(set(_extract_numbers(raw, excluded_spans=excluded))),
        "names": sorted({m.group(0) for m in _NAME_RE.finditer(raw) if m.group(0) not in _GENERIC_NAMES}),
    }


def _canonical_identifier(value: str) -> str | None:
    value = value.strip().strip("`'\"")
    value = value.rstrip(".,;:)")
    if not value or value in _GENERIC_NAMES or _DATE_RE.fullmatch(value):
        return None
    if " " in value and not re.search(r"(?i)\b(?:PR|Chainlink|issue)\s*#|\barXiv:\s*\d{4}\.\d{4,5}|\bRFC\s*\d+", value):
        return None
    if value.startswith("/"):
        # Single-segment slash tokens are usually artifacts from ordinary
        # compounds (``papers/arXiv`` -> ``/arXiv``). Keep only path-like
        # values with hierarchy, an extension, or another structural marker.
        if value.count("/") < 2 and "." not in value and "#" not in value:
            return None
    lowered = value.lower()
    if lowered.startswith("arxiv") and not re.search(r"\d{4}\.\d{4,5}", value):
        return None
    if re.match(r"(?i)^(?:pr|chainlink|issue)\b", value) and "#" not in value:
        return None
    if not re.search(r"[A-Za-z0-9]", value):
        return None
    return re.sub(r"\s+", " ", value)


def _canonical_number(value: str) -> str | None:
    match = _NUMBER_RE.fullmatch(value.strip())
    if not match:
        return None
    raw = re.sub(r"\s+", "", match.group(0).lower())
    if raw.startswith("+"):
        raw = raw[1:]
    for suffix in ("dimensions", "dimension", "d"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw or None


def _extract_numbers(text: str, *, excluded_spans: Sequence[tuple[int, int]]) -> list[str]:
    out: list[str] = []
    for match in _NUMBER_RE.finditer(text):
        if _overlaps_any(match.start(), match.end(), excluded_spans):
            continue
        if _looks_like_structural_index(text, match.start(), match.end()):
            continue
        num = _canonical_number(match.group(0))
        if num is None:
            continue
        if _looks_like_small_ordinal(text, match.start(), match.end()) and not ("." in num or "%" in num or "pp" in num):
            continue
        if num is not None:
            out.append(num)
    return _dedupe(out)


def _overlaps_any(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _looks_like_structural_index(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before == "[" and after == "]":
        return True
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    if not prefix.strip() and after in {".", ")", "]"}:
        return True
    return False


def _looks_like_small_ordinal(text: str, start: int, end: int) -> bool:
    token = text[start:end].strip().lstrip("+")
    if token not in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
        return False
    before = text[max(0, start - 3):start]
    after = text[end:min(len(text), end + 3)]
    if any(ch in before[-1:] + after[:1] for ch in "-–—"):
        return True
    if any(ch.isdigit() for ch in before + after):
        return False
    return True


def _number_is_date_component(number: str, dates: Sequence[str]) -> bool:
    stripped = number.lstrip("+-")
    for date in dates:
        parts = date.split("-")
        if stripped in parts or stripped.lstrip("0") in {part.lstrip("0") for part in parts}:
            return True
    return False


def _number_is_identifier_component(number: str, identifiers: Sequence[str]) -> bool:
    norm = _loose_norm(number)
    return bool(norm and any(norm and norm in _loose_norm(identifier) for identifier in identifiers))


def _artifact_equivalent(left: str, right: str) -> bool:
    left_num = _canonical_number(left)
    right_num = _canonical_number(right)
    if left_num is not None and right_num is not None:
        if left_num == right_num:
            return True
        left_core = left_num.rstrip("%pp")
        right_core = right_num.rstrip("%pp")
        if left_core == right_core:
            return True
        return _rounded_decimal_equivalent(left_core, right_core)
    left_arxiv = re.search(r"\d{4}\.\d{4,5}", left)
    right_arxiv = re.search(r"\d{4}\.\d{4,5}", right)
    if left_arxiv and right_arxiv:
        return left_arxiv.group(0) == right_arxiv.group(0)
    return False


def _rounded_decimal_equivalent(left: str, right: str) -> bool:
    try:
        lf = float(left)
        rf = float(right)
    except ValueError:
        return False
    return abs(lf - rf) < 0.051


def _derived_number_supported(candidate: str, source_numbers: Iterable[str]) -> bool:
    candidate_num = _canonical_number(candidate)
    if candidate_num is None:
        return False
    candidate_core = candidate_num.rstrip("%pp")
    try:
        candidate_float = abs(float(candidate_core))
    except ValueError:
        return False
    source_floats: list[float] = []
    for source in source_numbers:
        source_num = _canonical_number(source)
        if source_num is None:
            continue
        try:
            source_floats.append(float(source_num.rstrip("%pp")))
        except ValueError:
            continue
    for idx, left in enumerate(source_floats):
        for right in source_floats[idx + 1:]:
            if 0.0 <= left <= 1.0 and 0.0 <= right <= 1.0:
                if abs(abs(left - right) * 100.0 - candidate_float) < 0.051:
                    return True
    return False


def _identifier_collapse_should_hard_fail(retention: Mapping[str, Any], coverage: Mapping[str, Any]) -> bool:
    retained = retention.get("retained", {}).get("identifiers", [])
    missing = retention.get("missing", {}).get("identifiers", [])
    mutated = retention.get("mutated", {}).get("identifiers", [])
    identifier_total = len(retained) + len(missing) + len(mutated)
    if identifier_total == 0:
        identifier_score = 0.0
    else:
        identifier_score = (len(retained) + 0.25 * len(mutated)) / identifier_total
    # The hard gate is meant to catch bland mush, not summaries that preserve a
    # primary identifier and cover at least half the cluster while missing noisy
    # secondary symbols from heuristic annotations.
    return float(coverage.get("score", 0.0)) < 0.50 and identifier_score < 0.33


def _coverage_report(observation: str, source_atoms: list[dict[str, str]]) -> dict[str, Any]:
    if not source_atoms:
        return {"score": 1.0, "covered_atom_ids": [], "weak_atom_ids": []}
    covered: list[str] = []
    weak: list[str] = []
    obs_lower = observation.lower()
    for atom in source_atoms:
        markers = _atom_markers(atom["content"])
        if any(marker.lower() in obs_lower for marker in markers):
            covered.append(atom["atom_id"])
        else:
            weak.append(atom["atom_id"])
    return {
        "score": len(covered) / len(source_atoms),
        "covered_atom_ids": covered,
        "weak_atom_ids": weak,
    }


def _atom_markers(text: str) -> list[str]:
    markers = []
    artifacts = _extract_supported_artifacts(text)
    for key in ("identifiers", "dates", "names"):
        markers.extend(sorted(artifacts[key], key=len, reverse=True)[:3])
    if markers:
        return markers
    words = [w for w in re.findall(r"[A-Za-z0-9_-]{5,}", text) if w not in _GENERIC_NAMES]
    return words[:5]


def _retrieval_geometry(
    example: Mapping[str, Any], observation: str, embedding_fn: EmbeddingFn | None,
) -> dict[str, Any]:
    if embedding_fn is None:
        return {"score": 1.0, "skipped": True, "reason": "no embedding_fn supplied", "probe_errors": []}
    atoms = _source_atoms(example)
    probes = example.get("retrieval_probes") or []
    probe_queries = [p.get("query") for p in probes if isinstance(p, Mapping) and isinstance(p.get("query"), str)]
    if not atoms or not probe_queries:
        return {"score": 1.0, "skipped": True, "reason": "no atoms or probes", "probe_errors": []}
    obs_vec = _safe_embed(embedding_fn, observation)
    atom_vecs = [(atom, _safe_embed(embedding_fn, atom["content"])) for atom in atoms]
    if obs_vec is None or any(vec is None for _, vec in atom_vecs):
        return {"score": 1.0, "skipped": True, "reason": "embedding_fn failed", "probe_errors": []}

    errors: list[dict[str, float | str]] = []
    for query in probe_queries[:12]:
        q_vec = _safe_embed(embedding_fn, query)
        if q_vec is None:
            continue
        source_score = max(_cosine(q_vec, vec) for _, vec in atom_vecs if vec is not None)
        candidate_score = _cosine(q_vec, obs_vec)
        sq_error = (candidate_score - source_score) ** 2
        errors.append(
            {"query": query, "source_score": source_score, "candidate_score": candidate_score, "squared_error": sq_error}
        )
    if not errors:
        return {"score": 1.0, "skipped": True, "reason": "no embeddable probes", "probe_errors": []}
    mse = sum(float(e["squared_error"]) for e in errors) / len(errors)
    return {"score": max(0.0, 1.0 - mse), "skipped": False, "mse": mse, "probe_errors": errors}


def _safe_embed(fn: EmbeddingFn, text: str) -> tuple[float, ...] | None:
    try:
        return tuple(float(x) for x in fn(text))
    except Exception:
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _quality_report(observation: str) -> dict[str, Any]:
    """Report generic quality failures independent of source-symbol retention."""

    hits = _dedupe(m.group(0) for m in _META_CLUSTER_WRAPPER_RE.finditer(observation or ""))
    return {
        "score": 0.0 if hits else 1.0,
        "meta_cluster_wrapper": {
            "hits": hits,
            "guidance": (
                "State the remembered fact directly; do not describe the input "
                "cluster or atom count (for example, avoid 'these 3 atoms document ...')."
            ),
        },
    }


def _concision_score(observation: str) -> float:
    n = len(observation)
    if 80 <= n <= 650:
        return 1.0
    if n < 80:
        return max(0.4, n / 80)
    return max(0.2, 1.0 - ((n - 650) / 1000))


def _identifier_dense(example: Mapping[str, Any]) -> bool:
    strata = example.get("strata") or {}
    return bool(isinstance(strata, Mapping) and strata.get("identifier_dense"))


def _loose_norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out
