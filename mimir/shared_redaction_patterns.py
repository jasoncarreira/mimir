"""High-confidence bare secret patterns shared by durable and live logs."""

from __future__ import annotations

import re


CREDENTIAL_WORD_LITERALS = (
    "token", "apikey", "api_key", "api-key", "password", "passwd", "secret",
    "authorization", "credential", "key",
)
# Explicit private-key names and bare key, not dedupe_key, benign-key or monkey.
CREDENTIAL_WORD_PATTERN = (
    r"(?:token|api[_-]?key|password|passwd|secret|authorization|credentials?"
    r"|private[_-]?key|(?<![A-Za-z0-9_.:-])key(?![A-Za-z0-9_.-]))"
)

# No global entropy heuristic: image data, hashes and paths share that alphabet.
# Unknown blobs, including >=40-character values, are masked by the credential
# key grammars in both callers regardless of their alphabet or apparent entropy.
LOG_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Consume the body even when a subprocess emits an incomplete private key.
    re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
    ),
    re.compile(r"(?i)(\b(?:authorization\s*:\s*basic|(?:authorization\s*:\s*)?bearer)\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"\b(?:xoxe-|glpat-|sk_live_)[A-Za-z0-9_-]+"),
    # Use the live scrubber's minimum length for OpenAI-shaped keys everywhere.
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


# These patterns are registered independently by both log redactors, but their
# secret-shape definitions must stay identical. ``xapp`` and ``tvly`` retain
# hyphens in the body because issued Slack app tokens and Tavily ``tvly-dev``
# keys use hyphen-delimited components. Voyage ``pa`` bodies do not.
BARE_PROVIDER_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9_-])xapp-[0-9A-Za-z-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])tvly-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])pa-[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}"),
)

# JWT headers begin with the base64url encoding of ``{\"`` (``eyJ``). Preserve
# that diagnostic header while masking payload and signature.
JWT_PATTERN = re.compile(
    r"(\beyJ[A-Za-z0-9_-]{8,}\.)([A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}\b)"
)
