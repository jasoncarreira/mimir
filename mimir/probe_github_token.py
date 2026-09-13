"""Verify the current GITHUB_TOKEN without CLI or ambient HTTP credentials."""

from __future__ import annotations

import os

import requests


def probe() -> tuple[bool, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token.strip():
        return False, "GITHUB_TOKEN not set"
    try:
        with requests.Session() as session:
            # Ignore parent proxies, netrc credentials, and CA overrides.
            session.trust_env = False
            response = session.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != 200:
                return False, f"GitHub HTTP {response.status_code}"
            data = response.json()
            login = data.get("login") if isinstance(data, dict) else None
            if not isinstance(login, str) or not login.strip():
                return False, "GitHub response missing login"
            return True, f"GitHub authenticated as {login}"
    except Exception:
        # Request/JSON errors can contain headers or raw response content.
        return False, "GitHub probe failed"
