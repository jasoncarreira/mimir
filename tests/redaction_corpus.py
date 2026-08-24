"""Synthetic secret-bearing text shared by redactor tests."""

from __future__ import annotations


FAKE_SECRET = "0123456789abcdef0123456789abcdef"

COMPACT_HS256_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJhIjoiYiJ9.LEAKPROBEsignature"
RS256_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
    "0123456789abcdefghijklmnopq"
)

BARE_SECRET_TEXT_CORPUS = (
    "xapp-1-0123456789abcdefghij",
    "tvly-dev-0123456789abcdefghij",
    "pa-0123456789abcdefghij",
    "AIza" + FAKE_SECRET + "abc",
    COMPACT_HS256_JWT,
    RS256_JWT,
)

SECRET_TEXT_CORPUS = (
    "ghp_" + FAKE_SECRET,
    "sk-ant-" + FAKE_SECRET,
    "VOYAGE_API_KEY=pa-" + FAKE_SECRET,
    "TAVILY_API_KEY: tvly-" + FAKE_SECRET,
    "> X-API-Key: " + FAKE_SECRET,
    "secret: '" + FAKE_SECRET + "'",
    '{"api_key": "' + FAKE_SECRET + '"}',
    *BARE_SECRET_TEXT_CORPUS,
)
