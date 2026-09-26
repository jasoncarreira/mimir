"""Local content scanning for writes to external sinks."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .read_policy import text_contains_secret


@dataclass(frozen=True)
class OutboundFinding:
    """Non-reversible metadata describing one outbound privacy match."""

    detector: str
    kind: str
    match_length: int
    match_sha256: str


OutboundScan = list[OutboundFinding]

_PRIVATE_TERMS_FILE = "private-terms.txt"
_HASH_PREFIX_LENGTH = 12
_cache_lock = threading.Lock()
_cached_path: Path | None = None
_cached_signature: tuple[int, int] | None = None
_cached_terms: tuple[str, ...] = ()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_PREFIX_LENGTH]


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split()).casefold()


def _load_private_terms() -> tuple[str, ...]:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return ()
    path = Path(home).expanduser() / _PRIVATE_TERMS_FILE
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except (OSError, RuntimeError):
        signature = None

    global _cached_path, _cached_signature, _cached_terms
    with _cache_lock:
        if path == _cached_path and signature == _cached_signature:
            return _cached_terms
        terms: tuple[str, ...] = ()
        if signature is not None:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, RuntimeError):
                lines = []
            terms = tuple(dict.fromkeys(
                normalized
                for line in lines
                if line.strip() and not line.lstrip().startswith("#")
                if (normalized := _normalize_whitespace(line))
            ))
        _cached_path = path
        _cached_signature = signature
        _cached_terms = terms
        return terms


def _private_term_matches(text: str, term: str) -> Iterable[str]:
    folded = text.casefold()
    phrase_pattern = re.escape(term).replace(r"\ ", r"\s+")
    for match in re.finditer(phrase_pattern, folded):
        yield match.group(0)

    digits = "".join(character for character in term if character.isdigit())
    if len(digits) < 7:
        return
    digit_pattern = r"(?<!\d)" + r"\D*".join(map(re.escape, digits)) + r"(?!\d)"
    for match in re.finditer(digit_pattern, text):
        yield match.group(0)


def scan_outbound(
    texts: Iterable[str], *, tool: str, sink_category: str,
) -> OutboundScan:
    """Return privacy findings without retaining or returning matched values."""
    del tool, sink_category  # Reserved for detector-specific policy and diagnostics.
    findings: OutboundScan = []
    terms = _load_private_terms()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        if text_contains_secret(text):
            from .secret_scan import secret_matches

            matches = secret_matches(text) or {text}
            findings.extend(
                OutboundFinding(
                    detector="credential",
                    kind="credential",
                    match_length=len(matched),
                    match_sha256=_fingerprint(matched),
                )
                for matched in matches
            )
        seen_private: set[tuple[int, str]] = set()
        for term in terms:
            for matched in _private_term_matches(text, term):
                metadata = (len(matched), _fingerprint(matched))
                if metadata in seen_private:
                    continue
                seen_private.add(metadata)
                findings.append(OutboundFinding(
                    detector="private_term",
                    kind="private_term",
                    match_length=metadata[0],
                    match_sha256=metadata[1],
                ))
    return findings
