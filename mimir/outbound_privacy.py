"""Local content scanning for writes to external sinks."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .env import env_bool
from .read_policy import text_contains_secret


@dataclass(frozen=True)
class OutboundFinding:
    """Non-reversible metadata describing one outbound privacy match."""

    detector: str
    kind: str
    match_length: int
    match_sha256: str | None
    score: float | None = None


OutboundScan = list[OutboundFinding]

OUTBOX_PATTERNS = (
    "state/pollers/social-cli-*/outbox-*.yaml",
    "state/pollers/social-cli-*/outbox.yaml",
)
OUTBOX_CONTROL_PATTERNS = (
    "state/pollers/social-cli-*/config.yaml",
)

_PRIVATE_TERMS_FILE = "private-terms.txt"
_FINGERPRINT_KEY_FILE = ".outbound-privacy-key"
_FINGERPRINT_KEY_BYTES = 32
_HASH_PREFIX_LENGTH = 12
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
JEV_TIMEOUT_SECONDS = 3.0
JEV_PII_THRESHOLD = 0.5
JEV_PII_QUESTION = {
    "instructions": (
        "Does this outbound text reveal personal or private information about a "
        "specific real person that should not be published or sent to a third party? "
        "Count: home or mailing addresses, personal phone numbers, personal email "
        "addresses, government ID numbers, financial account or card numbers, medical "
        "or health details, private family matters, precise location or travel plans of "
        "a private individual, or contents of private messages. Do not count: public "
        "figures' public roles, business contact details published for that purpose, "
        "generic technical content, or information about no identifiable person."
    ),
    "criteria": {
        "true": "reveals private or personal information about an identifiable person",
        "false": "contains no private personal information",
    },
}
_JEV_MAX_TEXT_LENGTH = 4000
_JEV_MIN_TEXT_LENGTH = 20
_cache_lock = threading.Lock()
_cached_path: Path | None = None
_cached_signature: tuple[int, int] | None = None
_cached_terms: tuple[str, ...] = ()


def _matches_outbox_pattern(
    path: Path, home: Path, patterns: tuple[str, ...] | None = None,
) -> bool:
    try:
        relative_parts = path.relative_to(home).as_posix().split("/")
    except ValueError:
        return False
    for pattern in OUTBOX_PATTERNS if patterns is None else patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            continue
        pattern_parts = pattern_path.as_posix().split("/")
        if len(relative_parts) != len(pattern_parts):
            continue
        if all(
            fnmatch.fnmatchcase(part, pattern_part)
            for part, pattern_part in zip(relative_parts, pattern_parts)
        ):
            return True
    return False


def _matches_registered_path(
    path: Path | str, patterns: tuple[str, ...],
) -> bool:
    home_value = os.environ.get("MIMIR_HOME", "").strip()
    if not home_value:
        return False
    try:
        home = Path(home_value).expanduser().resolve(strict=False)
        requested = Path(path).expanduser()
        lexical = Path(os.path.abspath(requested))
        resolved = requested.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return any(
        _matches_outbox_pattern(candidate, home, patterns)
        for candidate in dict.fromkeys((lexical, resolved))
    )


def is_outbox_path(path: Path | str) -> bool:
    """Return whether either the lexical or resolved path is a registered outbox."""
    return _matches_registered_path(path, OUTBOX_PATTERNS)


def is_outbox_control_path(path: Path | str) -> bool:
    """Return whether a path controls which social-cli outbox is dispatched."""
    return _matches_registered_path(path, OUTBOX_CONTROL_PATTERNS)


def _load_fingerprint_key() -> bytes | None:
    home = os.environ.get("MIMIR_HOME", "").strip()
    if not home:
        return None
    path = Path(home).expanduser() / _FINGERPRINT_KEY_FILE
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
            )
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            try:
                os.fchmod(descriptor, 0o600)
                key = secrets.token_bytes(_FINGERPRINT_KEY_BYTES)
                written = 0
                while written < len(key):
                    count = os.write(descriptor, key[written:])
                    if count <= 0:
                        raise OSError("fingerprint key write made no progress")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            key = os.read(descriptor, _FINGERPRINT_KEY_BYTES + 1)
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError):
        return None
    return key if len(key) == _FINGERPRINT_KEY_BYTES else None


def _fingerprint(value: str) -> str | None:
    key = _load_fingerprint_key()
    if key is None:
        return None
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[
        :_HASH_PREFIX_LENGTH
    ]


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


def _jev_pii_score(text: str, key: str) -> tuple[float | None, str | None]:
    payload = {
        "model": JEV_MODEL,
        "state": text[:_JEV_MAX_TEXT_LENGTH],
        "questions": {
            "pii": {"type": "noul", **JEV_PII_QUESTION},
        },
    }
    request = urllib.request.Request(
        JEV_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=JEV_TIMEOUT_SECONDS) as response:  # noqa: S310
            answer = json.loads(response.read().decode("utf-8"))
    except TimeoutError:
        return None, "timeout"
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None, "http_error"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed_response"
    except Exception:
        return None, "request_failed"

    if not isinstance(answer, dict):
        return None, "malformed_response"
    answers = answer.get("answers")
    pii = answers.get("pii") if isinstance(answers, dict) else None
    if not isinstance(pii, dict) or set(pii) != {"type", "noul"}:
        return None, "malformed_response"
    score = pii.get("noul")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        return None, "malformed_response"
    if pii.get("type") != "noul":
        return None, "malformed_response"
    return float(score), None


def scan_jev_outbound(
    texts: Iterable[str], *, key: str,
) -> tuple[OutboundScan, tuple[str, ...]]:
    """Run only Jev network classification and return value-free outcomes."""
    findings: OutboundScan = []
    failures: list[str] = []
    for text in texts:
        if not isinstance(text, str) or len(text) < _JEV_MIN_TEXT_LENGTH:
            continue
        score, failure_reason = _jev_pii_score(text, key)
        if failure_reason is not None:
            failures.append(failure_reason)
            continue
        if score is not None and score >= JEV_PII_THRESHOLD:
            state = text[:_JEV_MAX_TEXT_LENGTH]
            findings.append(OutboundFinding(
                detector="pii",
                kind="pii",
                match_length=len(state),
                match_sha256=_fingerprint(state),
                score=score,
            ))
    return findings, tuple(failures)


def jev_detector_enabled() -> bool:
    """Return whether the operator supplied both Jev opt-in settings."""
    return env_bool("MIMIR_OUTBOUND_PII_JEV", False) and bool(
        os.environ.get("JEV_KEY", "").strip()
    )


def scan_outbound(
    texts: Iterable[str],
    *,
    tool: str,
    sink_category: str,
    emit_event: Callable[..., Any] | None = None,
    include_jev: bool = True,
    jev_candidates: list[str] | None = None,
) -> OutboundScan:
    """Return privacy findings without retaining or returning matched values."""
    del tool, sink_category  # Reserved for detector-specific policy and diagnostics.
    findings: OutboundScan = []
    terms = _load_private_terms()
    jev_key = os.environ.get("JEV_KEY", "").strip()
    jev_enabled = include_jev and jev_detector_enabled()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        local_finding = False
        if text_contains_secret(text):
            from .secret_scan import secret_matches

            matches = secret_matches(text) or {text}
            local_finding = True
            findings.extend(
                OutboundFinding(
                    detector="credential",
                    kind="credential",
                    match_length=len(matched),
                    match_sha256=_fingerprint(matched),
                )
                for matched in matches
            )
        seen_private: set[str] = set()
        for term in terms:
            for matched in _private_term_matches(text, term):
                if matched in seen_private:
                    continue
                seen_private.add(matched)
                metadata = (len(matched), _fingerprint(matched))
                local_finding = True
                findings.append(OutboundFinding(
                    detector="private_term",
                    kind="private_term",
                    match_length=metadata[0],
                    match_sha256=metadata[1],
                ))
        if local_finding:
            continue
        if jev_candidates is not None and len(text) >= _JEV_MIN_TEXT_LENGTH:
            jev_candidates.append(text)
        if not jev_enabled:
            continue
        jev_findings, failure_reasons = scan_jev_outbound((text,), key=jev_key)
        findings.extend(jev_findings)
        if emit_event is not None:
            for failure_reason in failure_reasons:
                emit_event("outbound_pii_check_failed", reason=failure_reason)
    return findings
