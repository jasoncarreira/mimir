"""High-confidence bare secret patterns shared by durable and live logs."""

from __future__ import annotations

import re


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
