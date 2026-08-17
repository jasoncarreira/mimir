"""Synthetic secret-bearing text shared by redactor tests."""

from __future__ import annotations


FAKE_SECRET = "0123456789abcdef0123456789abcdef"

SECRET_TEXT_CORPUS = (
    "ghp_" + FAKE_SECRET,
    "sk-ant-" + FAKE_SECRET,
    "VOYAGE_API_KEY=pa-" + FAKE_SECRET,
    "TAVILY_API_KEY: tvly-" + FAKE_SECRET,
    "> X-API-Key: " + FAKE_SECRET,
    "secret: '" + FAKE_SECRET + "'",
    '{"api_key": "' + FAKE_SECRET + '"}',
)
