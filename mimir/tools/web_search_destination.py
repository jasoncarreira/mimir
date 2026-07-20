"""Canonical operator configuration for the fixed web-search endpoint."""

from __future__ import annotations

import os


DEFAULT_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def web_search_url() -> str:
    """Return the configured search endpoint, defaulting empty values."""
    return os.environ.get("TAVILY_SEARCH_URL", "").strip() or DEFAULT_TAVILY_SEARCH_URL
