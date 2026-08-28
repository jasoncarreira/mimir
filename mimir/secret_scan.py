"""Commit-time secret detection shared by autonomous git-writing paths.

These patterns MIRROR the content allowlist in ``templates/git/pre-commit``
(the authoritative /mimir-home commit hook) — high-signal, length-floored
credential shapes, deliberately WITHOUT the bare ``token=`` / ``password=`` /
``api_key=`` forms. That distinction matters: ``mimir.redaction`` is a
log-masking policy whose contract explicitly permits false positives (it only
masks, never refuses), so it is unsafe as a *commit-refusal* gate — a bare
``token=placeholder`` in a generated doc/test would block otherwise-legitimate
work. A refusal gate must match what the commit hook actually enforces.

Keep this list in sync with ``templates/git/pre-commit`` PATTERNS.
"""

from __future__ import annotations

import re

# One entry per pre-commit PATTERNS line (same floors, same shapes).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer [A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj[-_][A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]+-v[0-9]+-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),           # OpenAI classic (base62, no hyphens)
    re.compile(r"tvly-[A-Za-z0-9_\-]{20,}"),     # Tavily API key
    re.compile(r"pa-[A-Za-z0-9_\-]{20,}"),       # Voyage API key
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),       # Google API key (39 chars total)
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),          # GitHub PAT (classic)
    re.compile(r"gho_[A-Za-z0-9]{30,}"),          # GitHub OAuth
    re.compile(r"gh[usr]_[A-Za-z0-9]{30,}"),      # GitHub user/server/app tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),  # GitHub fine-grained PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS access key
    re.compile(r"ASIA[0-9A-Z]{16}"),              # AWS STS temp key
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*[A-Za-z0-9/+=]{20,}"),
    re.compile(r'"refresh_token"\s*:\s*"[^"]{20,}"'),
    re.compile(r'"access_token"\s*:\s*"[^"]{20,}"'),
    re.compile(r'"client_secret"\s*:\s*"[^"]{20,}"'),
    re.compile(r"xox[bpasr]-[0-9A-Za-z-]{20,}"),  # Slack credential tokens
    re.compile(r"xapp-[0-9A-Za-z-]{20,}"),        # Slack app-level token
    re.compile(r"\b[A-Za-z0-9_-]{25,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
)


def secret_matches(text: str) -> set[str]:
    """Return the distinct secret-shaped substrings in ``text``."""
    return {
        match.group(0)
        for pattern in _SECRET_PATTERNS
        for match in pattern.finditer(text)
    }


def contains_secret(text: str) -> bool:
    """True if ``text`` contains a high-signal, secret-shaped credential.

    Suitable as a commit/push refusal gate: the patterns mirror the pre-commit
    hook, so a refusal here agrees with the commit-time policy and does not fire
    on the broad low-signal shapes the log redactor tolerates.
    """
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)
