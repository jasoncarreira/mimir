"""Operator-side, provenance-pinned Cultivate Docker installer.

No credential is placed on a command line or in a generated build context.
The Docker and GitHub boundaries are deliberately injectable for offline tests.
"""

from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import getpass
import hashlib
from importlib import metadata, resources
import json
from urllib.parse import quote
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request

import yaml

from mimir._reasoning_effort import validate_effort

from .repository import valid_repository_slug


# Tool defaults are explicit; repository declarations take precedence.
DEFAULT_SOURCE_REPO = "jasoncarreira/mimir"
DEFAULT_NEXTEST_VERSION = "0.9.148"
DEFAULT_UV_VERSION = "0.11.0"
DEFAULT_SAGA_MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}
DEFAULT_SAGA_EFFORT = "low"
LOCAL_MODEL = "voyageai/voyage-4-nano"
EMBEDDING_MODELS = {"local": ("local", LOCAL_MODEL, 1024, None),
                    "openai": ("openai", "text-embedding-3-small", 1536, "OPENAI_API_KEY"),
                    "voyage": ("voyage", "voyage-4-lite", 1024, "VOYAGE_API_KEY")}

PAT_HELP = (
    "Create a fine-grained PAT at https://github.com/settings/personal-access-tokens/new: "
    "resource owner = the owner of the target repository (the organization for an org repo; "
    "the org may require approval under Settings, Personal access tokens); "
    "repository access = only the target repository; Contents, Pull requests, Issues: Read and write; "
    "Actions, Commit statuses, Metadata: Read-only; optional Workflows: Read and write "
    "to push changes under .github/workflows/. Invite the bot as a Write collaborator on the repository "
    "and have the bot accept the invitation before continuing."
)
DISCORD_BOT_SETUP = """Discord bot setup:
1. Create an application and bot in the Discord Developer Portal at https://discord.com/developers/applications.
2. Under Bot, enable Message Content Intent (and Server Members Intent if your server requires member-role admission). The bridge also uses default message and reaction gateway intents.
3. Copy the bot token; enter it at the hidden DISCORD_TOKEN prompt.
4. In OAuth2, use the bot scope and View Channels, Send Messages, Read Message History, Add Reactions and Attach Files permissions. Invite via https://discord.com/oauth2/authorize?client_id=<application-id>&scope=bot&permissions=101440
5. Enable Developer Mode in Discord, right-click the alert text channel and copy its numeric channel ID.
"""
SLACK_BOT_SETUP = """Slack bot setup:
1. Create an app at https://api.slack.com/apps for your workspace.
2. Enable Socket Mode; create an app-level token (xapp-...) with connections:write.
3. Under OAuth & Permissions add bot scopes: chat:write, channels:history, groups:history, im:history, im:write, mpim:history, reactions:write, users:read, users:read.email, files:write. Subscribe to message.channels, message.groups, message.im, message.mpim, app_mention and reaction_added events used by the bridge.
4. Install the app to the workspace and copy the bot token (xoxb-...).
5. Invite the bot to the alert channel and copy its channel ID (starts with C or G).
"""
_PUSH_PROBE_FAILED = "CULTIVATE_PUSH_PROBE_FAILED"
_PUSH_PROBE_HELP = (
    "Token cannot push to the target repository. Recreate the fine-grained PAT with "
    "resource owner = the repository owner (the organization for an org repo), "
    "repository access = only the target repository, and Contents: Read and write; "
    "get organization approval if required."
)
_PUSH_PROBE_UNREACHABLE = "CULTIVATE_PUSH_PROBE_UNREACHABLE"
_PUSH_PROBE_UNREACHABLE_HELP = (
    "Push probe could not reach the remote (network or GitHub error); retry setup."
)
_CULTIVATE_DEFAULT_SKILLS = ("github-poller", "github-ci-watch", "github", "review", "tracker-poller")
_TRACKER_CHOICES = ("github-issues", "linear", "jira")
_TRACKER_ADAPTERS = {"github-issues": "github_issues", "linear": "linear", "jira": "jira"}
_JIRA_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}")
_JIRA_SITE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+")
_JIRA_PERMISSION_HELP = (
    "Jira service account id (accountId). The service account needs Browse Projects on each "
    "--jira-project, Add Comments, and Transition Issues when transitions or close_on_merge are set. "
    "The adapter also calls the user, group and project-role endpoints, GET /rest/api/3/user, "
    "GET /rest/api/3/user/groups (for --jira-trusted-group), GET /rest/api/3/project/{key}/role and "
    "GET /rest/api/3/project/{key}/role/{id} (for --jira-trusted-role), alongside "
    "POST /rest/api/3/search/jql, GET /rest/api/3/issue/{key}, POST /rest/api/3/issue/{key}/comment and "
    "GET/POST /rest/api/3/issue/{key}/transitions. Grant the service account read access to the "
    "configured groups and roles (Jira may require the \"Browse users and groups\" global permission or "
    "project administration) and verify that each endpoint returns 200 for it. A 401 or 403 fails closed: "
    "no Jira issue is delivered, and the poller writes the stderr code adapter_unavailable:jira:http_403."
)
_LINEAR_KEY_HELP = (
    "Read the Linear access token from a mode-600 file (never from argv). Use the access token of a Linear "
    "OAuth app installed with actor=app and the app:assignable scope; the bot is set as the issue's delegate."
)
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_DEBIAN_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+(?:=[0-9][A-Za-z0-9.+:~-]*)?")
_ALERT_ENV_KEYS = frozenset({"MIMIR_OPERATOR_ALERT_CHANNEL", "DISCORD_TOKEN", "SLACK_BOT_TOKEN",
                            "SLACK_APP_TOKEN", "MIMIR_DISCORD_ALLOWED_CHANNELS",
                            "MIMIR_DISCORD_REQUIRE_MENTION", "MIMIR_SLACK_ALLOWED_CHANNELS",
                            "MIMIR_SLACK_REQUIRE_MENTION"})


class SetupError(Exception):
    """Actionable error; never include an untrusted response body or a secret."""


def _installed_license() -> tuple[bytes, str]:
    """Read the wheel resource (or its editable-distribution source), never cwd."""
    dist = metadata.distribution("mimir-cultivate")
    resource = resources.files("mimir_cultivate").joinpath("LICENSE")
    if resource.is_file():
        return resource.read_bytes(), dist.version
    # Hatch editable installs expose the source package, with LICENSE beside it.
    source_license = Path(__file__).resolve().parent.parent / "LICENSE"
    if source_license.is_file():
        return source_license.read_bytes(), dist.version
    raise SetupError("Installed Cultivate license is missing; reinstall mimir-cultivate before setup.")


def _license_gate(args: argparse.Namespace) -> None:
    license_bytes, version = _installed_license()
    digest = hashlib.sha256(license_bytes).hexdigest()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    record = config / "mimir" / "cultivate-license-accepted.json"
    try:
        prior = json.loads(record.read_text())
    except (OSError, ValueError):
        prior = {}
    if isinstance(prior, dict) and prior.get("sha256") == digest:
        return
    print("Cultivate uses the Business Source License 1.1 (BSL 1.1), NOT core Mimir's MIT license.")
    print(license_bytes.decode("utf-8"))
    if not getattr(args, "accept_license", False):
        if not sys.stdin.isatty():
            raise SetupError("Review the license above, then pass --accept-license for non-interactive setup.")
        try:
            accepted = input("Accept this license? Type yes or accept: ").strip().lower()
        except EOFError:
            accepted = ""
        if accepted not in {"yes", "accept"}:
            raise SetupError("Cultivate license not accepted; nothing was written.")
    record.parent.mkdir(parents=True, exist_ok=True)
    _write(record, json.dumps({"sha256": digest, "version": version,
                              "accepted_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n",
           mode=0o600, replace=True)


def validate(name: str, value: str) -> str:
    """Validate fields before any side effect or command interpolation."""
    value = value.strip()
    if name in {"repo", "source_repo"}:
        ok = valid_repository_slug(value) and all(p not in {".", ".."} for p in value.split("/"))
    elif name in {"bot_login", "owner", "display_name"}:
        ok = bool(_NAME.fullmatch(value)) if name != "display_name" else bool(value) and "\n" not in value
    elif name in {"bot_email", "owner_email"}:
        ok = bool(_EMAIL.fullmatch(value))
    elif name == "branch":
        ok = bool(_BRANCH.fullmatch(value)) and ".." not in value and "//" not in value and not value.endswith("/")
    elif name == "model_spec":
        ok = bool(re.fullmatch(r"(openai|anthropic):[A-Za-z0-9_.-]+", value))
    elif name == "saga_model_spec":
        ok = bool(re.fullmatch(r"(openai|anthropic):[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", value))
        if ok and value.count(":") == 2:
            provider, _, effort = value.split(":")
            try:
                validate_effort(provider, effort)
            except ValueError as exc:
                raise SetupError(f"Invalid saga model spec: {exc}") from None
    elif name == "embedding":
        ok = value == "local" or bool(re.fullmatch(r"(openai|voyage):[A-Za-z0-9_.-]+", value))
    elif name == "review_tests":
        ok = value in {"targeted", "full", "none"}
    elif name == "auto_review":
        ok = value in {"all", "requested-only"}
    elif name == "worklink_gate":
        ok = value in {"local", "ci"}
    elif name in {"test_command", "quick_gate_command"}:
        ok = bool(value) and "\n" not in value and "\r" not in value
    elif name == "port":
        ok = value.isdigit() and 1 <= int(value) <= 65535
    elif name == "source_ref":
        ok = bool(re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9_./-]+", value)) and ".." not in value
    else:
        ok = bool(value) and not any(c in value for c in "\r\n\0")
    if not ok:
        raise SetupError(f"Invalid {name.replace('_', ' ')}; supply a valid {name.replace('_', ' ')} (see --help).")
    return value


def _tracker_settings(args: argparse.Namespace, previous: dict) -> tuple[list[str], dict[str, object]]:
    """Resolve the tracker set and its non-secret settings; flags override, saved values persist."""
    requested = getattr(args, "tracker", None)
    if requested is None:
        saved = previous.get("trackers", [])
        if not isinstance(saved, list) or any(t not in _TRACKER_CHOICES for t in saved):
            raise SetupError("Invalid trackers in manifest.json; reconcile it before retrying.")
        trackers = sorted(set(saved))
    elif "none" in requested:
        if set(requested) != {"none"}:
            raise SetupError("--tracker none cannot be combined with another tracker.")
        trackers = []
    else:
        trackers = sorted(set(requested))
    saved_settings = previous.get("tracker_settings", {})
    if not isinstance(saved_settings, dict):
        raise SetupError("Invalid tracker settings in manifest.json; reconcile it before retrying.")
    settings: dict[str, object] = {}
    for key in ("jira_site", "jira_account_id"):
        value = getattr(args, key, None)
        value = saved_settings.get(key) if value is None else value
        if value is not None:
            if not isinstance(value, str):
                raise SetupError(f"Invalid {key.replace('_', ' ')} in manifest.json; reconcile it before retrying.")
            settings[key] = value.strip()
    for key in ("jira_projects", "jira_trusted_groups", "jira_trusted_roles"):
        value = getattr(args, key, None)
        value = saved_settings.get(key, []) if value is None else value
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SetupError(f"Invalid {key.replace('_', ' ')} in manifest.json; reconcile it before retrying.")
        settings[key] = sorted({v.strip() for v in value})
    if "jira" in trackers:
        site = settings.get("jira_site")
        if not site or not _JIRA_SITE.fullmatch(site):
            raise SetupError("Invalid Jira site; pass --jira-site as a host without scheme, such as example.atlassian.net.")
        account = settings.get("jira_account_id")
        if not account or not account.isprintable() or any(c.isspace() for c in account):
            raise SetupError("Invalid Jira account id; pass --jira-account-id (see --help for the required permissions).")
        if not settings["jira_projects"] or any(not _JIRA_PROJECT_KEY.fullmatch(k) for k in settings["jira_projects"]):
            raise SetupError("Invalid Jira projects; pass at least one --jira-project key such as PROJ.")
        trusted = settings["jira_trusted_groups"] + settings["jira_trusted_roles"]
        if not trusted or any(not v or not v.isprintable() for v in trusted):
            raise SetupError("Invalid Jira trust settings; pass --jira-trusted-group or --jira-trusted-role.")
    return trackers, settings


def _tracker_config(trackers: list[str], settings: dict[str, object]) -> dict[str, object] | None:
    """The setup-owned ``tracker:`` section of worklink.yaml, or None when no tracker is selected."""
    if not trackers:
        return None
    adapters: dict[str, object] = {}
    for name in trackers:
        if name == "jira":
            adapter: dict[str, object] = {"account_id": settings["jira_account_id"],
                                          "projects": settings["jira_projects"]}
            if settings["jira_trusted_groups"]:
                adapter["trusted_groups"] = settings["jira_trusted_groups"]
            if settings["jira_trusted_roles"]:
                adapter["trusted_project_roles"] = settings["jira_trusted_roles"]
            adapters["jira"] = adapter
        else:
            adapters[_TRACKER_ADAPTERS[name]] = {}
    return {"close_on_merge": False, "adapters": adapters}


def validate_system_packages(packages: list[str]) -> list[str]:
    """Validate apt arguments, including any values loaded from a previous manifest."""
    normalized = []
    for entry in packages:
        package = entry.strip() if isinstance(entry, str) else entry
        if not isinstance(package, str) or not _DEBIAN_PACKAGE.fullmatch(package):
            raise SetupError(f"Invalid system package {entry!r}; use comma-separated Debian package names, optionally =version.")
        normalized.append(package)
    return normalized


def _field(args: argparse.Namespace, name: str, explanation: str, *, default: str | None = None) -> str:
    value = getattr(args, name, None) or os.environ.get("MIMIR_CULTIVATE_" + name.upper())
    if value is None:
        if not sys.stdin.isatty():
            if default is None:
                raise SetupError(f"Missing {name.replace('_', ' ')}; pass --{name.replace('_', '-')} or MIMIR_CULTIVATE_{name.upper()}.")
            value = default
        else:
            print(explanation)
            value = input(f"{name.replace('_', ' ').title()} [{default or 'required'}]: ").strip() or default
    return validate(name, value or "")


def _secret(args: argparse.Namespace, name: str, explanation: str, *, previous: str | None = None) -> str:
    sources = [getattr(args, name + "_file", None), getattr(args, name + "_stdin", False)]
    if sources[0] is not None and sources[1]:
        raise SetupError(f"Choose only one source for {name.replace('_', ' ')}.")
    if sources[0] is not None:
        path = Path(sources[0])
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise SetupError(f"{name.replace('_', ' ')} file must exist and have mode 600 (chmod 600 <file>); no key was read.")
        value = path.read_text(encoding="utf-8").strip()
    elif sources[1]:
        value = sys.stdin.readline().strip()
    else:
        value = os.environ.get("MIMIR_CULTIVATE_" + name.upper()) or previous
        if value is None:
            if not sys.stdin.isatty():
                raise SetupError(f"Missing {name.replace('_', ' ')}; use --{name.replace('_', '-')}-file, --{name.replace('_', '-')}-stdin or MIMIR_CULTIVATE_{name.upper()}.")
            print(explanation)
            value = getpass.getpass(f"{name.replace('_', ' ').title()}: ")
    if not value or any(c in value for c in "\r\n\0"):
        raise SetupError(f"Invalid {name.replace('_', ' ')}; provide a single non-empty line.")
    return value


def _alert_settings(args: argparse.Namespace, owner: str, previous: dict, prior_env: dict[str, str]):
    from mimir.web_channels import web_channel_for_identity

    adapter = getattr(args, "operator_alert_adapter", None)
    if adapter is None:
        adapter = previous.get("operator_alert_adapter") if isinstance(previous, dict) else None
    if adapter is None:
        adapter = (input("Where should operator alerts go? [web/discord/slack/none] ").strip().lower() or "none") if sys.stdin.isatty() else "none"
    if not isinstance(adapter, str) or adapter not in {"web", "discord", "slack", "none"}:
        raise SetupError("Invalid operator alert adapter; choose web, discord, slack or none.")
    channel = None
    updates = {}
    if adapter in {"discord", "slack"}:
        print(DISCORD_BOT_SETUP if adapter == "discord" else SLACK_BOT_SETUP)
        channel = getattr(args, "operator_alert_channel_id", None) or (
            previous.get("operator_alert_channel_id") if previous.get("operator_alert_adapter") == adapter else None
        )
        if channel is None:
            if not sys.stdin.isatty():
                raise SetupError("Missing operator alert channel ID; pass --operator-alert-channel-id.")
            channel = input("Operator alert channel ID: ").strip()
        pattern = r"[0-9]{15,22}" if adapter == "discord" else r"[CG][A-Z0-9]{8,}"
        if not isinstance(channel, str) or not re.fullmatch(pattern, channel):
            raise SetupError("Invalid operator alert channel ID; check the channel ID for the chosen adapter.")
        names = ("discord_token",) if adapter == "discord" else ("slack_bot_token", "slack_app_token")
        for name in names:
            key = name.upper()
            value = _secret(args, name, f"{key}: copy it from your bot setup; input is hidden.", previous=prior_env.get(key))
            if (adapter == "discord" and any(c.isspace() for c in value)) or (
                name == "slack_bot_token" and not re.fullmatch(r"xoxb-[A-Za-z0-9-]+", value)
            ) or (name == "slack_app_token" and not re.fullmatch(r"xapp-[A-Za-z0-9-]+", value)):
                raise SetupError(f"Invalid {key} format; check the bot token.")
            updates[key] = value
        prefix = adapter.upper()
        updates.update({f"MIMIR_{prefix}_ALLOWED_CHANNELS": channel,
                        f"MIMIR_{prefix}_REQUIRE_MENTION": "true"})
        updates["MIMIR_OPERATOR_ALERT_CHANNEL"] = adapter + "-" + channel
    elif adapter == "web":
        updates["MIMIR_OPERATOR_ALERT_CHANNEL"] = web_channel_for_identity(owner)
    else:
        print("Operator alerts disabled; rerun setup with --operator-alert-adapter web, discord or slack to enable them.")
    return adapter, channel, updates


def _request(url: str, token: str, *, method: str = "GET") -> tuple[int, object]:
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mimir-cultivate-setup",
    }, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        # GitHub error bodies can reflect credentials; never include them in errors.
        return exc.code, {}
    except (OSError, ValueError) as exc:
        raise SetupError("GitHub connection failed; check network access and retry.") from None


def validate_token(token: str, bot: str, slug: str, request=_request) -> None:
    """Check identity, collaborator role and readable endpoints; push is probed after clone."""
    api = "https://api.github.com"
    status, user = request(api + "/user", token)
    if status != 200 or not isinstance(user, dict):
        raise SetupError("GitHub token authentication failed; create an unexpired fine-grained PAT. " + PAT_HELP)
    if str(user.get("login", "")).lower() != bot.lower():
        raise SetupError("GitHub token authenticates as a different login; create the PAT while signed in as the bot.")
    status, repo = request(f"{api}/repos/{slug}", token)
    if status != 200 or not isinstance(repo, dict):
        raise SetupError("Bot cannot access the target repository; invite it as a Write collaborator, accept the invitation, and select this repository on the PAT.")
    if repo.get("permissions", {}).get("push") is not True:
        raise SetupError("Bot lacks a Write collaborator role; invite the bot as a Write collaborator and accept the invitation.")
    for route, label in (("pulls?per_page=1", "Pull requests: Read and write"),
                         ("actions/runs?per_page=1", "Actions: Read-only")):
        status, _ = request(f"{api}/repos/{slug}/{route}", token)
        if status != 200:
            raise SetupError(f"Bot cannot read {label}; enable that fine-grained PAT permission and retry. " + PAT_HELP)


def _content(slug: str, branch: str, path: str, token: str, request=_request) -> str | None:
    status, data = request(f"https://api.github.com/repos/{slug}/contents/{path}?ref={branch}", token)
    if status == 404:
        return None
    if status != 200 or not isinstance(data, dict) or data.get("encoding") != "base64":
        raise SetupError(f"Cannot read {path} on {branch}; check branch and Contents: Read and write.")
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (ValueError, KeyError, UnicodeError):
        raise SetupError(f"Cannot decode {path} from GitHub; check the repository contents.") from None


def detect_toolchain(files: dict[str, str | None]) -> tuple[str, str, str]:
    """Return (kind, exact version, suggested verification command)."""
    if files.get("rust-toolchain.toml") is not None or files.get("Cargo.toml") is not None:
        match = re.search(r'(?m)^channel\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', files.get("rust-toolchain.toml") or "")
        if not match:
            raise SetupError("Rust project has no exact rust-toolchain.toml channel; pin a release there.")
        return "rust", match.group(1), "cargo nextest run"
    if files.get("package.json") is not None:
        try:
            package = json.loads(files["package.json"] or "")
        except ValueError:
            raise SetupError("package.json is not valid JSON; fix it before setup.") from None
        match = re.fullmatch(r"(?:v)?([0-9]+\.[0-9]+\.[0-9]+)", str(package.get("engines", {}).get("node", "")))
        if not match:
            raise SetupError("Node project needs an exact engines.node pin in package.json (e.g. 22.16.0).")
        scripts = package.get("scripts") or {}
        command = "npm run verify" if "verify" in scripts else "npm test" if "test" in scripts else "npm run build"
        return "node", match.group(1), command
    if files.get("pyproject.toml") is not None or files.get("uv.lock") is not None:
        pin = (files.get(".python-version") or "").strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin):
            raise SetupError("Python project needs an exact .python-version pin (e.g. 3.11.11).")
        return "python", pin, "uv run pytest -q"
    raise SetupError("No supported toolchain found; add rust-toolchain.toml, package.json or pyproject.toml.")


def _verify_script(slug: str, branch: str, token: str, request=_request) -> str | None:
    status, tree = request(f"https://api.github.com/repos/{slug}/git/trees/{quote(branch, safe='')}?recursive=1", token)
    if status != 200 or not isinstance(tree, dict) or tree.get("truncated"):
        raise SetupError("Cannot inspect executable verification scripts; check Contents permission and repository tree size.")
    executable = {row.get("path") for row in tree.get("tree", [])
                  if row.get("type") == "blob" and row.get("mode") == "100755"}
    for path in ("tools/verify", "scripts/verify", "tools/verify.sh", "scripts/verify.sh", "verify", "verify.sh"):
        if path in executable:
            return "./" + path
    return None


def _tool_pin(files: dict[str, str | None], tool: str, default: str) -> str:
    patterns = {
        "nextest": r"cargo-nextest@([0-9]+\.[0-9]+\.[0-9]+)",
        "uv": r"uv-version:\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)",
    }
    pins = {match for text in files.values() if text for match in re.findall(patterns[tool], text)}
    if len(pins) > 1:
        raise SetupError(f"Conflicting {tool} version pins; reconcile the repository declarations.")
    return next(iter(pins), default)


def render_layer(kind: str, version: str, root_image: str, *,
                 nextest_version: str = DEFAULT_NEXTEST_VERSION,
                 uv_version: str = DEFAULT_UV_VERSION,
                 system_packages: list[str] | None = None) -> str:
    """Keep version-pinned tools on /usr/local/bin, without changing shell policy."""
    packages = validate_system_packages(system_packages or [])
    if kind == "rust":
        install = ("ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/workspace/.cargo\n"
                   "ENV CARGO_INSTALL_ROOT=/opt/cargo\n"
                   "ENV CARGO_HOME=/opt/cargo\n"
                   "RUN set -eu; arch=$(dpkg --print-architecture); case $arch in amd64) triple=x86_64-unknown-linux-gnu;; "
                   "arm64) triple=aarch64-unknown-linux-gnu;; *) exit 2;; esac && "
                   "apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev binutils pkg-config && "
                   "rm -rf /var/lib/apt/lists/* && mkdir -p /opt/rustup /opt/cargo && "
                   "curl -fsSLo /tmp/rustup-init https://static.rust-lang.org/rustup/archive/1.28.2/$triple/rustup-init && "
                   "chmod 755 /tmp/rustup-init && /tmp/rustup-init -y "
                   f"--no-modify-path --default-toolchain {version} && rm /tmp/rustup-init && "
                   "chmod -R a+rX /opt/rustup /opt/cargo && "
                   "for proxy in /opt/cargo/bin/*; do [ -e \"$proxy\" ] && "
                   "ln -s \"$proxy\" \"/usr/local/bin/${proxy##*/}\" || exit 1; done && "
                   "case $arch in amd64) platform=linux;; arm64) platform=linux-arm;; *) exit 2;; esac && "
                   f"curl -fsSLo /tmp/nextest.tar.gz https://get.nexte.st/{nextest_version}/$platform && "
                   "tar -xzf /tmp/nextest.tar.gz -C /usr/local/bin cargo-nextest && rm /tmp/nextest.tar.gz\n"
                   "RUN set -eu; install -d -o worklink -g worklink -m 0700 /var/cache/mimir-cargo\n"
                   "ENV CARGO_HOME=/var/cache/mimir-cargo\n")
    elif kind == "node":
        install = (f"RUN set -eu; arch=$(dpkg --print-architecture); case $arch in amd64) arch=x64;; arm64) ;; *) exit 2;; esac && "
                    "apt-get update && apt-get install -y --no-install-recommends g++ make python3 && "
                    "rm -rf /var/lib/apt/lists/* && "
                    f"curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v{version}/node-v{version}-linux-$arch.tar.xz "
                   "&& mkdir -p /opt/node && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 "
                   "&& ln -sf /opt/node/bin/node /usr/local/bin/node && ln -sf /opt/node/bin/npm /usr/local/bin/npm "
                   "&& ln -sf /opt/node/bin/npx /usr/local/bin/npx && rm /tmp/node.tar.xz\n")
    else:
        install = ("ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python\n"
                    "RUN set -eu; apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && "
                    "rm -rf /var/lib/apt/lists/* && "
                    f"curl -fsSLo /tmp/uv-install.sh https://astral.sh/uv/{uv_version}/install.sh && "
                   "sh /tmp/uv-install.sh && rm /tmp/uv-install.sh && "
                   "cp /root/.local/bin/uv /usr/local/bin/uv && cp /root/.local/bin/uvx /usr/local/bin/uvx\n"
                   f"RUN set -eu; uv python install {version} && chmod -R a+rX /opt/uv-python\n")
    extra = ("RUN set -eu; apt-get update && apt-get install -y --no-install-recommends "
             + " ".join(packages) + " && rm -rf /var/lib/apt/lists/*\n") if packages else ""
    return f"FROM {root_image}\nUSER root\n{extra}{install}RUN set -eu; mkdir -p /workspace /workspace/pr-leases /workspace/worklink && chown -R 1001:1001 /workspace\nUSER root\n"


def render_compose(template: str, layer_tag: str, kind: str, repo_name: str) -> str:
    """Modify the scaffold structurally; fail closed on incompatible shapes."""
    try:
        # Scaffold's prose header contains a shell printf example with a literal
        # newline; parse the document from its top-level services key instead.
        document = re.search(r"(?m)^services:\s*$", template)
        if document is None:
            raise KeyError("services")
        doc = yaml.safe_load(template[document.start():])
        agent = doc["services"]["agent"]
        environment = agent["environment"]
        if not isinstance(environment, dict) or not isinstance(agent["build"], (dict, str)):
            raise TypeError
        del agent["build"]
        agent["image"] = layer_tag
        passthrough = "UV_PYTHON_INSTALL_DIR" if kind == "python" else "RUSTUP_HOME,CARGO_HOME" if kind == "rust" else ""
        environment.update({"MIMIR_ACP_ENABLED": "1", "MIMIR_PR_CHECKOUT_LEASE_ROOT": "/workspace/pr-leases",
                            "MIMIR_SHELL_PASS_ENV": passthrough, "MIMIR_ENABLE_CLAUDE_CODE": "0",
                            "MIMIR_HOME": "/home/mimir/agent", "MIMIR_PLUGINS": "cultivate",
                            "XDG_CACHE_HOME": "/home/mimir/.cache",
                            "MIMIR_WORKLINK_AGENT_ID": "cultivate-worklink"})
        environment["WORKLINK_REPO"] = "/workspace/worklink/base/" + repo_name
        volumes = ["home:/home/mimir/agent", "cache:/home/mimir/.cache", "workspace:/workspace",
                   "pr-leases:/workspace/pr-leases", "worklink:/workspace/worklink"]
        agent["volumes"] = volumes
        agent.setdefault("depends_on", {})["init"] = {"condition": "service_completed_successfully"}
        doc["services"]["init"] = {
            "image": layer_tag, "user": "root", "entrypoint": ["/bin/sh", "-c"],
            "command": ["mkdir -p /workspace/pr-leases /workspace/worklink /workspace/.worklink /home/mimir/agent /home/mimir/.cache "
                        "&& chown -R 1001:1001 /workspace /home/mimir/agent /home/mimir/.cache "
                        "&& chmod 0755 /workspace/.worklink"],
            "volumes": volumes,
        }
        doc["volumes"] = {name: None for name in ("home", "cache", "workspace", "pr-leases", "worklink")}
        return yaml.safe_dump(doc, sort_keys=False)
    except (KeyError, TypeError, AttributeError, yaml.YAMLError):
        raise SetupError("Incompatible scaffold Compose template; expected agent build and environment mapping.") from None


def _write(path: Path, text: str, *, mode: int = 0o644, replace: bool = False) -> bool:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise SetupError(f"Refusing non-regular deployment file {path}; move it aside and retry.")
        if path.read_text() == text:
            path.chmod(mode)
            return False
        if not replace:
            raise SetupError(f"{path} already differs; reconcile it manually before retrying.")
        path.write_text(text)
    else:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), "w") as out:
            out.write(text)
    path.chmod(mode)
    return True


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _previous_image(deploy: Path, previous: dict) -> tuple[str, str] | None:
    """Recover the original image from the saved build, including legacy installs."""
    build = deploy / "build.sh"
    if not build.is_file() or build.is_symlink():
        return None
    text = build.read_text()
    root = re.search(r"(?m)^docker build -t mimir-cultivate-root:([0-9a-f]{40}) ", text)
    layer = re.search(r"(?m)^docker build -t (mimir-cultivate-agent:([0-9a-f]{40})-[a-z0-9-]+) -f Dockerfile\.toolchain \.\s*$", text)
    if not root or not layer or root.group(1) != layer.group(2):
        return None
    sha, tag = root.group(1), layer.group(1)
    if (previous.get("source_sha", sha) != sha or previous.get("layer_tag", tag) != tag):
        return None
    return sha, tag


def _previous_provision_compose(source: str, constants: dict[str, object]) -> str:
    """Recover the pinned provision override without executing historical setup."""
    if "_PROVISION_COMPOSE" in constants:
        value = constants["_PROVISION_COMPOSE"]
        if not isinstance(value, str):
            raise ValueError("Invalid provision Compose constant")
        return value

    # Before the constant existed, setup wrote this literal inline. Match the
    # exact writer and destination, not a coincidentally named string elsewhere.
    values = []
    for function in ast.parse(source).body:
        if not isinstance(function, ast.FunctionDef) or function.name != "setup":
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_write" and len(node.args) >= 2):
                continue
            destination = node.args[0]
            if not (isinstance(destination, ast.BinOp) and isinstance(destination.op, ast.Div)
                    and isinstance(destination.left, ast.Name) and destination.left.id == "deploy"
                    and isinstance(destination.right, ast.Constant)
                    and destination.right.value == "compose.provision.yml"):
                continue
            value = ast.literal_eval(node.args[1])
            if not isinstance(value, str):
                raise ValueError("Invalid inline provision Compose template")
            values.append(value)
    if len(values) != 1:
        raise ValueError("Missing or ambiguous inline provision Compose template")
    return values[0]


def _previous_generated(source_repo: str, token: str, old_image: tuple[str, str],
                        previous: dict, port: int, request) -> dict[str, str]:
    """Render legacy files with the pinned source that originally produced them."""
    sha, layer_tag = old_image
    error = (f"Cannot recover previous generated templates for {sha}; "
             "restore that source commit or reconcile the deployment before retrying.")
    try:
        setup_source = _content(source_repo, sha, "plugins/cultivate/mimir_cultivate/setup.py", token, request)
        scaffold_source = _content(source_repo, sha, "mimir/scaffold_docker.py", token, request)
    except SetupError:
        raise SetupError(error) from None
    if setup_source is None or scaffold_source is None:
        raise SetupError(error)

    try:
        def extract(source: str, names: set[str]) -> tuple[dict[str, object], dict[str, ast.FunctionDef]]:
            constants = {}
            functions = {}
            for node in ast.parse(source).body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id in names:
                            constants[target.id] = ast.literal_eval(node.value)
                elif isinstance(node, ast.FunctionDef) and node.name in names:
                    functions[node.name] = node
            return constants, functions

        setup_constants, setup_functions = extract(setup_source, {"_PROVISION", "_PROVISION_COMPOSE", "render_compose"})
        scaffold_constants, scaffold_functions = extract(scaffold_source, {
            "_COMPOSE_YML_TEMPLATE_PYPI", "_DEFAULT_MODE", "_MODES", "render_compose_yml"})
        provision = setup_constants["_PROVISION"]
        provision_compose = _previous_provision_compose(setup_source, setup_constants)
        template = scaffold_constants["_COMPOSE_YML_TEMPLATE_PYPI"]
        if not all(isinstance(value, str) for value in (provision, provision_compose, template)):
            raise ValueError
        # Compile only the two pinned renderers, never the modules' top-level code.
        namespace = {"re": re, "yaml": yaml, "SetupError": SetupError, **scaffold_constants}
        for function in (setup_functions["render_compose"], scaffold_functions["render_compose_yml"]):
            module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                      function], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), f"{sha}:renderer", "exec"), namespace)
        compose = namespace["render_compose_yml"](service_name="agent", web_port=port, mode="pypi")
        compose = namespace["render_compose"](compose, layer_tag, previous["toolchain"], previous["slug"].split("/")[1])
        return {"compose.yml": compose, "provision.py": provision,
                "compose.provision.yml": provision_compose, ".gitignore": "*\n"}
    except (KeyError, IndexError, NameError, SyntaxError, TypeError, ValueError, SetupError):
        raise SetupError(error) from None


def _check_generated(deploy: Path, generated: dict[str, str], previous: dict,
                     old_image: tuple[str, str] | None, *, force: bool,
                     source_repo: str, token: str, request) -> None:
    """Validate *all* owned files before replacing any of them."""
    hashes = previous.get("generated_sha256")
    if hashes is not None and (not isinstance(hashes, dict) or
                               any(not isinstance(hashes.get(name), str) or
                                   not re.fullmatch(r"[0-9a-f]{64}", hashes[name]) for name in generated)):
        raise SetupError("Invalid generated file hashes in manifest.json; reconcile it before retrying.")
    legacy = {}
    if hashes is None and (deploy / "compose.yml").exists() and not force:
        if old_image is None:
            raise SetupError("Cannot verify previous generated files; reconcile build.sh before retrying.")
        compose_path = deploy / "compose.yml"
        content = compose_path.read_text()
        match = re.search(r"127\.0\.0\.1:(\d+):8080", content)
        if not match:
            raise SetupError(f"{compose_path} already differs; reconcile it manually before retrying.")
        legacy = _previous_generated(source_repo, token, old_image, previous, int(match.group(1)), request)
    for name, text in generated.items():
        path = deploy / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SetupError(f"Refusing non-regular deployment file {path}; move it aside and retry.")
        if path.exists() and not force:
            content = path.read_text()
            trusted = (_digest(content) == hashes[name] if hashes is not None else
                       content == legacy.get(name) if legacy else True)
            if not trusted:
                raise SetupError(f"{path} already differs; reconcile it manually before retrying.")
        elif hashes is not None and not force:
            raise SetupError(f"{path} already differs; reconcile it manually before retrying.")


_PR_LEASE_PROBE = """\
import json
from mimir_cultivate.repository import configured_pr_checkout_lease_root, active_pr_checkout_lease_for_path
root = configured_pr_checkout_lease_root()
leases = []
for path in root.iterdir():
 if path.is_dir():
  lease = active_pr_checkout_lease_for_path(path)
  if lease is not None:
   leases.append(f'{lease.canonical_repo}#{lease.pr_number}')
print(json.dumps(sorted(leases)))
"""


def _check_in_flight(deploy: Path, run) -> None:
    """A running agent must be idle before setup can recreate or restart it."""
    status = run(["docker", "compose", "exec", "-T", "-u", "mimir", "agent",
                  "mimir", "worklink", "status"], cwd=deploy)
    rows = status.splitlines()
    if rows == ["No Worklink leaf runs or labeled issues."]:
        active = []
    elif rows and rows[0].startswith("ISSUE  STATE "):
        active = []
        for row in rows[1:]:
            fields = row.split()
            if row.startswith("  disagreement:"):
                continue
            if len(fields) < 2 or not fields[0].isdigit():
                raise SetupError("Cannot read Worklink status; retry after inspecting the agent.")
            if fields[1] != "clean":
                active.append(f"Worklink #{fields[0]} ({fields[1]})")
    else:
        raise SetupError("Cannot read Worklink status; retry after inspecting the agent.")
    try:
        leases = json.loads(run(["docker", "compose", "exec", "-T", "-u", "mimir", "agent",
                                 "python", "-c", _PR_LEASE_PROBE], cwd=deploy))
    except ValueError:
        raise SetupError("Cannot read PR checkout leases; retry after inspecting the agent.") from None
    if not isinstance(leases, list) or any(not isinstance(lease, str) for lease in leases):
        raise SetupError("Cannot read PR checkout leases; retry after inspecting the agent.")
    active.extend(f"PR checkout lease {lease}" for lease in leases)
    if active:
        raise SetupError("Agent is busy: " + ", ".join(active) + "; retry when idle or use --force.")


def toolchain_test_env(kind: str) -> dict[str, str]:
    """Settings persisted for both the leaf build and its evidence gate."""
    from .worklink.backends.opencode import validate_test_env

    env = {'rust': {'RUSTUP_HOME': '/opt/rustup', 'CARGO_HOME': '/var/cache/mimir-cargo'},
           'python': {'UV_PYTHON_INSTALL_DIR': '/opt/uv-python'}, 'node': {}}[kind]
    validate_test_env(env)
    return env


def _provision_worklink(home: Path, entry: dict, *, base: Path, checkout: Path,
                        git_auth: list[str], force: bool = False,
                        toolchain: str | None = None, tracker: dict | None = None) -> bool:
    """Reconcile the dedicated, controller-owned base and Worklink settings."""
    from .worklink.backends.registry import BackendRegistry, WorklinkConfig, _derive_bash_allowlist

    origin = entry["origin"]
    branch = entry["base_branch"]
    if (base.resolve() == checkout.resolve() or base.resolve().is_relative_to(checkout.resolve())
            or checkout.resolve().is_relative_to(base.resolve())
            or base.resolve().is_relative_to(Path('/workspace/pr-leases'))):
        raise RuntimeError("Worklink base overlaps the live checkout or lease root; choose a dedicated base")
    if base.is_symlink() or base.parent.is_symlink():
        raise RuntimeError(f"Worklink base {base} is a symlink; move it aside and retry")
    if base.exists() and (not base.is_dir() or not (base / '.git').is_dir()):
        raise RuntimeError(f"Worklink base {base} is not a clone; move it aside and retry")

    def git(*args: str) -> str:
        result = subprocess.run(['git', '-C', str(base), *args], capture_output=True,
                                text=True, check=False)
        if result.returncode:
            raise RuntimeError(f"Worklink base {base}: git {args[0]} failed; inspect the base and retry")
        return result.stdout.strip()

    created = not base.exists()
    if created:
        base.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(git_auth + ['clone', '--branch', branch, origin, str(base)],
                                capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"Cannot clone Worklink base {base}; check origin and base branch")
    if git('config', '--local', '--get', 'remote.origin.url') != origin:
        raise RuntimeError(f"Worklink base {base} origin disagrees with repositories.yaml; move it aside or reconcile it manually")
    if git('symbolic-ref', '--short', 'HEAD') != branch:
        raise RuntimeError(f"Worklink base {base} is not on {branch}; check out the declared base branch manually")
    if git('status', '--porcelain=v1', '--untracked-files=all'):
        raise RuntimeError(f"Worklink base {base} is dirty; clean it manually and retry setup")
    # The controller later fetches origin from this clone, including private repos.
    helper = git_auth[-1].split('=', 1)[1]
    git('config', '--local', 'credential.helper', '')
    git('config', '--local', 'credential.https://github.com.helper', helper)
    for key, env_key in (('user.name', 'GH_USER_NAME'), ('user.email', 'GH_USER_EMAIL')):
        value = os.environ.get(env_key, '').strip()
        if not value:
            raise RuntimeError(f'Worklink base identity unconfigured: {env_key}')
        current = subprocess.run(['git', '-C', str(base), 'config', '--local', '--get', key],
                                 capture_output=True, text=True, check=False)
        if current.returncode not in (0, 1):
            raise RuntimeError(f'Worklink base {base}: git config {key} failed')
        if current.stdout.strip() != value:
            git('config', '--local', key, value)
            created = True

    path = home / 'worklink.yaml'
    config = {'repository': entry['slug'], 'defaults': {
        'backend': 'opencode', 'compute_backend': 'local_subprocess',
        'allow_autonomous_local_subprocess': True,
        'base_branch': branch, 'test_command': entry['test_command'],
        'max_concurrent': 2, 'timeout_s': 5400, 'reaper_ttl_s': 86400,
    }}
    try:
        allowlist = list(_derive_bash_allowlist(entry['test_command']))
    except ValueError:
        # Custom verification scripts cannot safely be widened to a runner
        # wildcard; grant only the declared command and Git operations.
        allowlist = ['git *', entry['test_command']]
    config['backends'] = {'opencode': {'bash_allowlist': allowlist,
                                     'test_env': toolchain_test_env(toolchain) if toolchain else {}}}
    if tracker:
        config['tracker'] = tracker
    text = yaml.safe_dump(config, sort_keys=False)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Worklink config {path} must be a regular file; move it aside")
    if path.exists() and path.read_text() != text and not force:
        raise RuntimeError(f"Worklink config {path} differs; preserve operator edits or rerun with --force")
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        created = True
    BackendRegistry(WorklinkConfig.load(path))
    store = home / '.chainlink'
    if store.is_symlink() or (store.exists() and not store.is_dir()):
        raise RuntimeError(f"Chainlink store {store} must be a real directory; move it aside and retry")
    if not store.exists():
        try:
            result = subprocess.run(['chainlink', 'init', '-q'], cwd=home,
                                    capture_output=True, check=False)
        except OSError:
            raise RuntimeError(f"Chainlink init could not run in {home}; check the CLI and retry") from None
        if result.returncode or store.is_symlink() or not store.is_dir():
            raise RuntimeError(f"Chainlink init failed in {home}; inspect .chainlink and retry")
        created = True
    agent = store / 'agent.json'
    if agent.is_symlink() or (agent.exists() and not agent.is_file()):
        raise RuntimeError(f"Chainlink agent identity {agent} must be a regular file")
    if not agent.exists():
        result = subprocess.run(['chainlink', 'agent', 'init', 'cultivate-worklink'], cwd=home,
                                capture_output=True, check=False)
        if result.returncode or not agent.is_file():
            raise RuntimeError(f"Chainlink agent init failed in {home}; inspect .chainlink and retry")
        created = True
    return created


def _env(deploy: Path, updates: dict[str, str], *, manage_alerts: bool = False) -> str:
    path = deploy / "compose.env"
    existing = path.read_text() if path.exists() else ""
    lines = existing.splitlines()
    current = dict(line.split("=", 1) for line in lines if re.match(r"^[A-Z_][A-Z0-9_]*=", line))
    if manage_alerts:
        lines = [line for line in lines if line.split("=", 1)[0] not in _ALERT_ENV_KEYS]
    for key, value in updates.items():
        if key in _ALERT_ENV_KEYS and manage_alerts:
            lines.append(f"{key}={value}")
        elif key not in current:
            lines.append(f"{key}={value}")
        elif current[key] != value and key != "MIMIR_API_KEY":
            raise SetupError(f"Existing compose.env conflicts on {key}; resolve the operator setting before retrying.")
    _write(path, "\n".join(lines) + "\n", mode=0o600, replace=True)
    return current.get("MIMIR_API_KEY") or updates["MIMIR_API_KEY"]


def _saga_settings(spec: str, embedding: str) -> dict[str, dict[str, object]]:
    provider, model, *suffix = validate("saga_model_spec", spec).split(":")
    effort = suffix[0] if suffix else (DEFAULT_SAGA_EFFORT if model == DEFAULT_SAGA_MODELS[provider] else None)
    kind, _, dim, key_env = EMBEDDING_MODELS[embedding.split(":", 1)[0]]
    embed_model = LOCAL_MODEL if embedding == "local" else embedding.split(":", 1)[1]
    llm: dict[str, object] = {"provider": "anthropic" if provider == "anthropic" else "openai_compat",
                              "model": model, "api_key_env": provider.upper() + "_API_KEY"}
    # Fresh installs must omit temperature regardless of model or effort.
    # Custom sampling is retained only for existing operator configurations.
    llm["sampling"] = "default"
    if effort:
        llm["reasoning_effort"] = effort
    if provider == "openai":
        llm["url"] = "https://api.openai.com/v1/chat/completions"
    emb: dict[str, object] = {"provider": kind, "model": embed_model, "dimensions": dim}
    if key_env:
        emb["api_key_env"] = key_env
        emb["url"] = f"https://api.{'voyageai' if kind == 'voyage' else 'openai'}.com/v1/embeddings"
    if kind == "voyage":
        emb["send_input_type"] = True
    return {"llm": llm, "embedding": emb}


def _provision_saga(home: Path, spec: str, embedding: str, reembed: bool = False,
                    *, prefetch=None, repair=None) -> bool:
    """Reconcile only installer-owned sections; leave all other TOML bytes intact."""
    from mimir.saga._config_io import install_saga_config
    from mimir.saga.embeddings import LocalProvider
    from mimir.saga.reembed import reembed as saga_reembed

    settings = _saga_settings(spec, embedding)
    path = home / "saga.toml"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("saga.toml must be a regular file; reconcile it manually")
    original = path.read_text() if path.exists() else ""
    existing = tomllib.loads(original)
    # Only the byte-for-byte core scaffold is known to be unedited. Its
    # claude_code default is unusable in Cultivate's SDK-disabled image.
    from mimir.commands.setup import _default_saga_toml
    core_scaffold = bool(original) and original == _default_saga_toml(home)
    marker = "# Cultivate-managed saga models\n"
    embedding_signature = re.search(r"(?m)^# Cultivate embedding sha256:([0-9a-f]{64})$", original)
    signed_embedding = (embedding_signature is not None and
                        embedding_signature.group(1) == hashlib.sha256(
                            json.dumps(existing.get("embedding", {}), sort_keys=True).encode()).hexdigest())
    db = home / ".mimir" / "saga.db"
    for section, fields in settings.items():
        old = existing.get(section, {})
        if old and (not isinstance(old, dict) or any(k in old and old[k] != v for k, v in fields.items())):
            if section == "embedding" and (signed_embedding or core_scaffold and db.exists()) and not reembed:
                raise RuntimeError("Changing embedding provider requires --reembed (stop the agent first), or run mimir saga-reembed offline")
            if not core_scaffold and (section != "embedding" or not reembed or not signed_embedding):
                raise RuntimeError(f"existing saga.toml [{section}] conflicts; reconcile the operator setting manually")
    old_embedding = existing.get("embedding", {})
    changed_embedding = bool(old_embedding and any(old_embedding.get(k) != v for k, v in settings["embedding"].items()))
    if db.exists() and changed_embedding and not reembed:
        raise RuntimeError("Changing embedding provider requires --reembed (stop the agent first), or run mimir saga-reembed after reconciling saga.toml")
    if db.exists() and not old_embedding and not reembed:
        raise RuntimeError("Existing saga.db requires --reembed before selecting an embedding provider")

    text = original
    for section, fields in settings.items():
        if existing.get(section) == fields or (existing.get(section) and not (core_scaffold or section == "embedding" and changed_embedding)):
            continue
        if existing.get(section) and (core_scaffold or section == "embedding" and changed_embedding):
            # Only a previously Cultivate-managed section can be replaced.
            text = re.sub(r"(?ms)^\[" + section + r"\]\n.*?(?=^\[|\Z)", "", text)
            if section == "embedding":
                text = re.sub(r"(?m)^# Cultivate embedding sha256:[0-9a-f]{64}\n", "", text)
        if section == "embedding":
            text += "# Cultivate embedding sha256:" + hashlib.sha256(
                json.dumps(fields, sort_keys=True).encode()).hexdigest() + "\n"
        text += ("\n" if text and not text.endswith("\n\n") else "") + f"[{section}]\n"
        for key, value in fields.items():
            text += f"{key} = {json.dumps(value)}\n"
    if (not original or core_scaffold) and text:
        text = marker + text
    # Download to the shared persistent cache before publishing a local config.
    if embedding == "local":
        if prefetch is None:
            prefetch = lambda: LocalProvider(model_name=LOCAL_MODEL, dimensions=1024)._load_model()
        prefetch()
    if text != original:
        path.write_text(text)
    if reembed and db.exists():
        install_saga_config(path)
        report = (repair or saga_reembed)(db, force=changed_embedding or not old_embedding, progress=lambda _: None)
        if any(value for name, value in report.items() if name.endswith("_unrepaired")):
            raise RuntimeError("Saga re-embed incomplete; restore missing summaries and run mimir saga-reembed before starting the agent")
    return text != original


def _port() -> int:
    for port in range(8090, 65536):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SetupError("No free loopback port found; pass --port after freeing a port.")


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=1200, check=False)
    if result.returncode:
        # Docker/gh may reflect a secret in their error output. Never relay it.
        if _PUSH_PROBE_FAILED in result.stdout.splitlines():
            raise SetupError(_PUSH_PROBE_HELP)
        if _PUSH_PROBE_UNREACHABLE in result.stdout.splitlines():
            raise SetupError(_PUSH_PROBE_UNREACHABLE_HELP)
        for name in _CULTIVATE_DEFAULT_SKILLS:
            if "CULTIVATE_SKILL_MODIFIED:" + name in result.stdout.splitlines():
                raise SetupError(f"Modified Cultivate skill cultivate:{name}; reconcile it or run "
                                 f"mimir skills install cultivate:{name} --restore "
                                 "(or rerun setup with --force to back up and restore it).")
        raise SetupError(f"{args[0]} operation failed (exit {result.returncode}); inspect the deployment without publishing secrets.")
    return result.stdout.strip()


def _http(url: str, *, key: str | None = None, data: dict | None = None) -> dict:
    headers = {"X-API-Key": key} if key else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers,
        data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _agent_exec(deploy: Path, *argv: str) -> str:
    """Run agent-state checks with the agent's uid and HOME, not image root."""
    return _run(["docker", "compose", "exec", "-T", "-u", "mimir", "agent", *argv], cwd=deploy)


# The worklink uid cannot traverse the controller's private venv. Resolve there
# as mimir, then run a stdlib-only probe with the system interpreter as worklink.
_OPENCODE_RESOLVE_PROBE = """\
try:
 from mimir_cultivate.worklink.backends.opencode import resolve_opencode_binary
 print(resolve_opencode_binary())
except (ImportError, OSError, ValueError):
 print('opencode_missing')
"""
_OPENCODE_WORKER_PROBE = """\
import subprocess
import sys
import tempfile
try:
 with tempfile.TemporaryDirectory(prefix='mimir-opencode-probe-') as home:
  env = {
   'USER': 'worklink', 'LOGNAME': 'worklink', 'SHELL': '/bin/sh',
   'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
   'HOME': home, 'XDG_CONFIG_HOME': home + '/.config',
   'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
   'OPENCODE_CONFIG': home + '/.config/opencode/opencode.json',
  }
  result = subprocess.run([sys.argv[1], '--version'], env=env, cwd='/tmp',
   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
  print('ok' if result.returncode == 0 else 'opencode_missing')
except (OSError, subprocess.TimeoutExpired, ValueError):
 print('opencode_missing')
"""
_OPENCODE_MODEL_PROBE = """\
import subprocess
import sys
import tempfile
try:
 with tempfile.TemporaryDirectory(prefix='mimir-opencode-probe-') as home:
  env = {
   'USER': 'worklink', 'LOGNAME': 'worklink', 'SHELL': '/bin/sh',
   'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
   'HOME': home, 'XDG_CONFIG_HOME': home + '/.config',
   'XDG_DATA_HOME': home + '/.local/share', 'XDG_CACHE_HOME': home + '/.cache',
   'OPENCODE_CONFIG': home + '/.config/opencode/opencode.json',
  }
  # The catalog command requires a configured provider. This is an inert
  # placeholder, never the operator's API key and never used for a completion.
  env[sys.argv[2].upper() + '_API_KEY'] = 'model-catalog-probe'
  result = subprocess.run([sys.argv[1], 'models', sys.argv[2]], env=env, cwd='/tmp',
   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
  print('ok' if result.returncode == 0 and sys.argv[3] in result.stdout.splitlines()
        else 'opencode_model_unknown')
except (OSError, subprocess.TimeoutExpired, ValueError):
 print('opencode_model_unknown')
"""

_TOOLCHAIN_WORKER_PROBE = """\
import json
from pathlib import Path
import subprocess
import sys
import tempfile

configured, kind, version = sys.argv[1:]
env = json.loads(configured)
runtime_home = env['HOME']
commands = {
 'rust': [('rustc', ['rustc', '--version']), ('cargo', ['cargo', '--version']),
          ('cargo-fmt', ['cargo', 'fmt', '--version']),
          ('cargo-clippy', ['cargo', 'clippy', '--version']),
          ('cargo-nextest', ['cargo', 'nextest', '--version'])],
 'node': [('node', ['node', '--version']), ('npm', ['npm', '--version']),
          ('npx', ['npx', '--version'])],
 'python': [('uv', ['uv', '--version'])],
}[kind]
with tempfile.TemporaryDirectory(prefix='mimir-toolchain-probe-') as home:
 env = {key: value.replace(runtime_home, home) for key, value in env.items()}
 if kind == 'rust':
  try:
   with tempfile.NamedTemporaryFile(dir=env['CARGO_HOME'], prefix='setup-write-'):
    pass
  except (KeyError, OSError):
   print('toolchain_missing: cargo')
   sys.exit(0)
 if kind == 'python':
  commands.append(('python', ['uv', 'python', 'find', '--managed-python', '--no-python-downloads', '--no-project', version]))
 for tool, argv in commands:
  try:
   result = subprocess.run(argv, env=env, cwd='/tmp', stdin=subprocess.DEVNULL,
    capture_output=True, text=True, timeout=30)
   if result.returncode or (tool == 'rustc' and result.stdout.split()[:2] != ['rustc', version]) or (tool == 'node' and result.stdout.strip() != 'v' + version):
    print('toolchain_missing: ' + tool)
    break
   if tool == 'python':
    binary = result.stdout.strip()
    if not binary.startswith('/opt/uv-python/'):
     print('toolchain_missing: python')
     break
    result = subprocess.run([binary, '--version'], env=env, cwd='/tmp',
     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != 'Python ' + version:
     print('toolchain_missing: python')
     break
  except (OSError, subprocess.TimeoutExpired, ValueError):
   print('toolchain_missing: ' + tool)
   break
 else:
  # Version checks do not exercise the native linker. Build in the disposable
  # gate HOME, with exactly the same env used above, and execute the result.
  source = Path(home) / ('main.rs' if kind == 'rust' else 'main.cc' if kind == 'node' else 'main.c')
  source.write_text('fn main() {}\\n' if kind == 'rust' else
                    'int main() { return 0; }\\n')
  compiler = {'rust': 'rustc', 'node': 'c++', 'python': 'cc'}[kind]
  binary = str(Path(home) / 'probe')
  try:
   built = subprocess.run([compiler, *(['--edition', '2021'] if kind == 'rust' else []),
                           '-o', binary, str(source)], env=env, cwd=home,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
   if built.returncode == 0:
    built = subprocess.run([binary], env=env, cwd=home,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
   print('ok' if built.returncode == 0 else 'toolchain_missing: ' +
         ('cc' if kind == 'rust' else compiler))
  except (OSError, subprocess.TimeoutExpired, ValueError):
   print('toolchain_missing: ' + ('cc' if kind == 'rust' else compiler))
"""


def toolchain_probe_environment(config) -> dict[str, str]:
    """Use the gate builder and persisted settings, never probe-only tool vars."""
    from .worklink.backends.opencode import _permission_override, validate_test_env
    from .worklink.compute import WorkSpec, _enabled_child_env

    settings = config.backend_settings.get('opencode', {})
    test_env = settings.get('test_env', {})
    validate_test_env(test_env)
    spec = WorkSpec(issue_id=0, attempt=1, repo_url='', base_ref='', branch='',
                    prompt='', rules=None, test_command='', backend='opencode', timeout_s=30,
                    env={'OPENCODE_PERMISSION': _permission_override(settings.get('bash_allowlist', [])),
                         **test_env}, backend_config={'pass_env': tuple(test_env)})
    return _enabled_child_env(spec, 'setup-toolchain-probe')


_TOOLCHAIN_ENV_PROBE = """\
import json
from pathlib import Path
from mimir_cultivate.setup import toolchain_probe_environment
from mimir_cultivate.worklink.backends.registry import WorklinkConfig
print(json.dumps(toolchain_probe_environment(WorklinkConfig.load(Path('/home/mimir/agent/worklink.yaml')))))
"""


def _verify_toolchain(deploy: Path, kind: str, version: str) -> None:
    configured = _agent_exec(deploy, '/home/mimir/venv/bin/python', '-c', _TOOLCHAIN_ENV_PROBE)
    check = _run(["docker", "compose", "exec", "-T", "-u", "worklink", "-w", "/tmp",
                  "agent", "/usr/local/bin/python", "-c", _TOOLCHAIN_WORKER_PROBE,
                  configured, kind, version], cwd=deploy)
    if check != "ok":
        tools = {"rust": {"rustc", "cargo", "cargo-fmt", "cargo-clippy", "cargo-nextest", "cc"},
                 "node": {"node", "npm", "npx", "c++"}, "python": {"uv", "python", "cc"}}
        reason = check if check in {"toolchain_missing: " + tool for tool in tools[kind]} else "toolchain_missing: " + kind
        raise SetupError(f"Leaf toolchain verification failed ({reason}); rebuild the agent image and check its toolchain installation.")


def _verify_system_packages(deploy: Path, packages: list[str]) -> None:
    for package in validate_system_packages(packages):
        name = package.split("=", 1)[0]
        try:
            status = _run(["docker", "compose", "exec", "-T", "-u", "worklink", "-w", "/tmp",
                           "agent", "dpkg-query", "-W", "-f=${Status}", name], cwd=deploy)
        except SetupError:
            status = ""
        if status != "install ok installed":
            raise SetupError(f"System package verification failed (system_package_missing: {name}); rebuild the agent image and check its package installation.")


def _verify_opencode_binary(deploy: Path) -> str:
    binary = _agent_exec(deploy, "/home/mimir/venv/bin/python", "-c", _OPENCODE_RESOLVE_PROBE)
    check = "opencode_missing"
    if binary.startswith("/") and "\n" not in binary and "\x00" not in binary:
        check = _run(["docker", "compose", "exec", "-T", "-u", "worklink", "-w", "/tmp",
                      "agent", "/usr/local/bin/python", "-c", _OPENCODE_WORKER_PROBE, binary], cwd=deploy)
    if check != "ok":
        raise SetupError("OpenCode leaf executable verification failed (opencode_missing); rebuild the agent image and check its OpenCode installation.")
    return binary


def _verify_opencode_model(deploy: Path, binary: str, model: str) -> None:
    # Only the provider/model crosses into the isolated worker probe, never the
    # configured credential or the controller's private OpenCode config.
    if not re.fullmatch(r"(openai|anthropic)/[A-Za-z0-9_.-]+", model):
        raise SetupError("OpenCode model verification failed (opencode_model_unknown); check the configured provider/model.")
    provider = model.split("/", 1)[0]
    check = _run(["docker", "compose", "exec", "-T", "-u", "worklink", "-w", "/tmp",
                  "agent", "/usr/local/bin/python", "-c", _OPENCODE_MODEL_PROBE,
                  binary, provider, model], cwd=deploy)
    if check != "ok":
        raise SetupError(f"OpenCode model verification failed (opencode_model_unknown): {model}; update the agent image or choose a listed model.")


def _verify_alert(url: str, owner_key: str, adapter: str) -> None:
    if adapter == "none":
        return
    history_url = url + "/api/v1/chat/history"
    before = _http(history_url, key=owner_key).get("data", {}).get("messages", []) if adapter == "web" else []
    prior_ids = {m.get("message_id") for m in before}
    try:
        result = _http(url + "/api/v1/chat/operator-alert-check", key=owner_key, data={})
    except urllib.error.HTTPError as exc:
        # The server returns fixed reason codes; never relay a remote body.
        reason = "alert_bridge_unconnected" if exc.code == 409 else "alert_send_failed"
        raise SetupError(f"Operator alert verification failed ({reason}).") from None
    except (OSError, ValueError):
        raise SetupError("Operator alert verification failed (alert_send_failed).") from None
    if not result.get("ok") or result.get("data", {}).get("sent") is not True:
        raise SetupError("Operator alert verification failed (alert_send_failed).")
    if adapter == "web":
        for attempt in range(10):
            messages = _http(history_url, key=owner_key).get("data", {}).get("messages", [])
            if any(m.get("role") == "assistant" and m.get("text") == "Cultivate setup: operator alerts enabled"
                   and m.get("message_id") not in prior_ids for m in messages):
                return
            time.sleep(1)
        raise SetupError("Operator alert verification failed (alert_send_failed): web history missing.")


def _verify(deploy: Path, port: int, slug: str, owner_key: str, kind: str, version: str,
             system_packages: list[str] | None = None, alert_adapter: str = "none",
             *, trackers: tuple[str, ...] = ()) -> None:
    url = f"http://127.0.0.1:{port}"
    for attempt in range(60):
        try:
            if _http(url + "/health").get("ok") is True:
                break
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise SetupError("/health did not become ready; inspect docker compose logs.")
    rows = json.loads(_agent_exec(deploy, "mimir", "plugins", "list", "--json"))
    if not any(row.get("name") == "cultivate" and row.get("enabled") is True for row in rows):
        raise SetupError("mimir plugins list did not show cultivate enabled; check process MIMIR_PLUGINS.")
    # Check current binding, not historical logs (a resolved alert may remain
    # in the log forever after an operator fixes the checkout).
    binding = _agent_exec(deploy, "python", "-c",
                    "import json; from mimir_cultivate.repository import repo_binding_startup_alerts; "
                    "print(json.dumps(repo_binding_startup_alerts()))")
    if json.loads(binding):
        raise SetupError("Repository binding attention required; check checkout origin and repositories.yaml.")
    # The executor answers from its own image/service environment, never from a
    # root included in a controller request. Also detects a stale executor image.
    root_check = _agent_exec(deploy, "python", "-c",
        "import asyncio\n"
        "from mimir_cultivate.worklink.worker_client import (verify_executor_identity, "
        "CheckoutRootMismatchError, StaleWorkerExecutorError)\n"
        "try:\n asyncio.run(verify_executor_identity())\n print('ok')\n"
        "except CheckoutRootMismatchError:\n print('checkout_root_mismatch')\n"
        "except StaleWorkerExecutorError:\n print('executor_stale')\n"
        "except (ConnectionResetError, PermissionError):\n print('executor_peer_rejected')\n"
        "except (OSError, RuntimeError, ValueError):\n print('executor_unreachable')")
    root_errors = {
        "checkout_root_mismatch": "Worklink checkout root mismatch; align WORKLINK_CHECKOUT_ROOT in the controller and executor service environments.",
        "executor_stale": "Worklink executor is stale; rebuild the image and restart the container.",
        "executor_peer_rejected": "Worklink executor rejected the agent peer; inspect its service and socket permissions.",
        "executor_unreachable": "Worklink executor is unreachable; inspect its service and socket.",
    }
    if root_check != "ok":
        raise SetupError(root_errors.get(root_check, "Worklink executor verification returned an unexpected result; inspect the executor service."))
    identity_check = _agent_exec(deploy, "python", "-c",
        "import os, subprocess\n"
        "repo = os.environ.get('WORKLINK_REPO', '')\n"
        "expected = (os.environ.get('GH_USER_NAME', '').strip(), os.environ.get('GH_USER_EMAIL', '').strip())\n"
        "def local(key):\n"
        " result = subprocess.run(['git', '-C', repo, 'config', '--local', '--get', key], capture_output=True, text=True)\n"
        " if result.returncode not in (0, 1):\n"
        "  raise RuntimeError('base_identity_unreadable')\n"
        " return result.stdout.strip() if result.returncode == 0 else ''\n"
        "try:\n"
        " if not repo:\n"
        "  raise RuntimeError('base_identity_unreadable')\n"
        " actual = (local('user.name'), local('user.email'))\n"
        " print('ok' if all(expected) and actual == expected else 'base_identity_mismatch')\n"
        "except (OSError, RuntimeError):\n"
        " print('base_identity_unreadable')")
    if identity_check == "base_identity_unreadable":
        raise SetupError("Worklink base identity unreadable (base_identity_unreadable); check that the runtime user can read the base clone's local Git config.")
    if identity_check != "ok":
        raise SetupError("Worklink base identity mismatch (base_identity_mismatch); check the base clone's local user.name/user.email and the bot identity in compose.env.")
    _verify_toolchain(deploy, kind, version)
    _verify_system_packages(deploy, system_packages or [])
    binary = _verify_opencode_binary(deploy)
    # Run in the agent container with its actual HOME, config and compose env.
    # Only a stable reason code crosses the Docker boundary, never the bundle.
    auth_check = _agent_exec(deploy, "python", "-c",
        "from mimir_cultivate.opencode import resolve_opencode_invocation, OpenCodeAuthError, OpenCodeConfigError\n"
        "try:\n resolve_opencode_invocation()\n print('ok')\n"
        "except (OpenCodeAuthError, OpenCodeConfigError) as exc:\n print(exc.reason_code)")
    if auth_check != "ok":
        raise SetupError(
            "OpenCode credential resolution failed (" + (
                auth_check if auth_check in {"auth_missing", "auth_invalid", "auth_malformed", "auth_unreadable"}
                else "unavailable"
            ) + "); check the agent container's model API key or Codex OAuth login and OpenCode config."
        )
    model = _agent_exec(deploy, "python", "-c",
        "from mimir_cultivate.opencode import resolve_opencode_invocation; "
        "print(resolve_opencode_invocation().model)")
    _verify_opencode_model(deploy, binary, model)
    schedule = _http(url + "/api/v1/scheduler", key=owner_key)
    pollers = {row.get("name") for row in schedule.get("data", {}).get("pollers", [])
               if row.get("next_run_at")}
    for poller in ("github-activity", "github-ci-watch", "worklink-ready-queue",
                   *(("tracker-poller",) if trackers else ())):
        if poller not in pollers:
            raise SetupError(f"{poller} was not scheduled; inspect poller configuration and restart.")
    # POST only acknowledges enqueue. Confirm the assistant reply in history.
    history_url = url + "/api/v1/chat/history"
    prior = _http(history_url, key=owner_key).get("data", {}).get("messages", [])
    prior_ids = {m.get("message_id") for m in prior}
    response = _http(url + "/api/v1/chat", key=owner_key, data={"content": "Reply with a short setup canary greeting."})
    if not response.get("ok"):
        raise SetupError("Canary chat was not accepted; check owner web-key roles.")
    for attempt in range(60):
        history = _http(history_url, key=owner_key)
        messages = history.get("data", {}).get("messages", [])
        if any(m.get("role") == "assistant" and m.get("text") and m.get("message_id") not in prior_ids
               for m in messages):
            break
        time.sleep(2)
    else:
        raise SetupError("Canary chat was accepted but no assistant reply arrived; inspect model access and logs.")
    _verify_alert(url, owner_key, alert_adapter)


def add_argparse(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = sub.add_parser("cultivate", help="Set up a Cultivate agent.")
    setup = parser.add_subparsers(dest="cultivate_action").add_parser("setup", help="Guide and verify a pinned Docker deployment.")
    for name, help_text in (
        ("repo", "GitHub owner/name of the target repository."),
        ("branch", "Base branch on the target repository (default: main)."),
        ("bot-login", "GitHub login of the invited bot collaborator."),
        ("bot-email", "Git commit email configured for the bot."),
        ("owner", "Canonical owner identity for authorized web chat."),
        ("display-name", "Owner's human-readable name."),
        ("owner-email", "Owner's email identity."),
        ("model-spec", "provider:model (openai:... or anthropic:...)."),
        ("saga-model-spec", "Fast/cheap memory model (openai:model[:effort] or anthropic:model[:effort])."),
        ("embedding", "local, openai:model, or voyage:model (default: local)."),
        ("deploy", "Absolute deployment directory (default: ./cultivate-agent)."),
        ("port", "Free loopback port (default: next free from 8090)."),
        ("test-command", "Verification command for target repo; default detected from repo."),
        ("review-tests", "Review test policy: targeted (default), full, or none; CI supplies full-suite evidence."),
        ("auto-review", "Automatic PR reviews: all (default) or requested-only; omit to preserve the saved mode."),
        ("worklink-gate", "Worklink gate mode: local (default) or ci; omit to preserve the saved mode."),
        ("quick-gate-command", "Optional quick verification command for ci mode; omit to preserve the saved command."),
        ("system-packages", "Extra Debian packages for the toolchain layer, comma-separated (optionally name=version); use none to clear, omit to preserve the saved list."),
        ("source-ref", "Pushed Mimir source ref (default: refs/heads/feature/plugins)."),
        ("source-repo", "Mimir source owner/name (default: " + DEFAULT_SOURCE_REPO + ")."),
    ):
        setup.add_argument("--" + name, help=help_text)
    for name in ("github-token", "model-key", "saga-model-key", "embedding-key"):
        setup.add_argument("--" + name + "-file", help="Read secret from a mode-600 file (never from argv).")
        setup.add_argument("--" + name + "-stdin", action="store_true", help="Read one secret line from stdin.")
    setup.add_argument("--tracker", action="append", choices=_TRACKER_CHOICES + ("none",),
                       help="Triage issues assigned to the bot from this tracker (repeatable: github-issues, linear, jira); "
                            "installs the tracker-poller skill and writes the tracker: section of worklink.yaml. "
                            "none clears the saved set; omit to preserve it. Edits to tracker: made by hand need --force on a rerun.")
    setup.add_argument("--jira-site", help="Jira Cloud host without scheme, such as example.atlassian.net (written to JIRA_SITE).")
    setup.add_argument("--jira-account-id", help=_JIRA_PERMISSION_HELP)
    setup.add_argument("--jira-project", dest="jira_projects", action="append", help="Jira project key to poll (repeatable).")
    setup.add_argument("--jira-trusted-group", dest="jira_trusted_groups", action="append",
                       help="Jira group whose members are trusted (repeatable).")
    setup.add_argument("--jira-trusted-role", dest="jira_trusted_roles", action="append",
                       help="Jira project role whose members are trusted (repeatable).")
    setup.add_argument("--linear-api-key-file", help=_LINEAR_KEY_HELP)
    setup.add_argument("--linear-api-key-stdin", action="store_true", help="Read the Linear access token from one stdin line.")
    for name, what in (("jira-email", "Jira service account email"), ("jira-api-token", "Jira service account API token")):
        setup.add_argument("--" + name + "-file", help=f"Read the {what} from a mode-600 file (never from argv).")
        setup.add_argument("--" + name + "-stdin", action="store_true", help=f"Read the {what} from one stdin line.")
    setup.add_argument("--operator-alert-adapter", choices=("web", "discord", "slack", "none"),
                       help="Alert destination (unattended default: none; saved on rerun).")
    setup.add_argument("--operator-alert-channel-id", help="Discord numeric or Slack C/G alert channel ID.")
    for name in ("discord-token", "slack-bot-token", "slack-app-token"):
        setup.add_argument("--" + name + "-file", help="Read the bot credential from a mode-600 file.")
        setup.add_argument("--" + name + "-stdin", action="store_true", help="Read the bot credential from stdin.")
    setup.add_argument("--accept-license", action="store_true", help="Explicitly accept the shipped Cultivate BSL 1.1 license for unattended setup.")
    setup.add_argument("--rotate-web-key", action="store_true", help="Replace the owner's existing web key intentionally.")
    setup.add_argument("--reembed", action="store_true", help="Re-embed existing Saga vectors offline when changing embeddings.")
    setup.add_argument("--no-start", action="store_true", help="Build and provision but leave the agent stopped; start it later from the deployment directory.")
    setup.add_argument("--force", action="store_true", help="Override generated-file edits and in-flight checks; replace an edited worklink.yaml and back up/restore modified Cultivate skills.")
    return parser


def dispatch(args: argparse.Namespace) -> int:
    if getattr(args, "cultivate_action", None) != "setup":
        print("Use `mimir cultivate setup --help` for guided setup.")
        return 2
    try:
        setup(args)
    except (SetupError, OSError, subprocess.TimeoutExpired) as exc:
        # Never interpolate arbitrary third-party exception text: it may contain secrets.
        message = str(exc) if isinstance(exc, SetupError) else "I/O timed out or failed; check deployment permissions and retry."
        print(f"setup: {message}", file=sys.stderr)
        return 1
    return 0


def setup(args: argparse.Namespace, *, request=_request, run=_run, verify=_verify) -> None:
    requested_packages = getattr(args, "system_packages", None)
    if requested_packages is not None:
        system_packages = ([] if requested_packages.strip() == "none"
                           else validate_system_packages(requested_packages.split(",")))
    if getattr(args, "worklink_gate", None) is not None:
        validate("worklink_gate", args.worklink_gate)
    if getattr(args, "auto_review", None) is not None:
        validate("auto_review", args.auto_review)
    if getattr(args, "quick_gate_command", None) is not None:
        validate("quick_gate_command", args.quick_gate_command)
        if getattr(args, "worklink_gate", None) == "local":
            raise SetupError("--quick-gate-command is only valid with --worklink-gate ci.")
    _license_gate(args)
    repo = _field(args, "repo", "Target owner/name: find it in the GitHub repository URL; used for clone and policy.")
    branch = _field(args, "branch", "Base branch: select the repository's default branch on GitHub; used for PRs.", default="main")
    bot = _field(args, "bot_login", "Bot login: create a separate GitHub account; used to check token identity.")
    bot_email = _field(args, "bot_email", "Bot commit email: find it under the bot's GitHub email settings; used by git.")
    owner = _field(args, "owner", "Owner canonical name: choose a stable identifier for admin and chat authorization.")
    display = _field(args, "display_name", "Owner display name: choose the name shown in chat.")
    owner_email = _field(args, "owner_email", "Owner email: your email address, used for identity reconciliation.")
    print(PAT_HELP)
    deploy = Path(_field(args, "deploy", "Deployment directory: choose a private local directory for Docker files and web key.", default=str(Path.cwd() / "cultivate-agent"))).expanduser().resolve()
    manifest_path = deploy / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    except (OSError, ValueError):
        raise SetupError("Cannot read previous settings from manifest.json; reconcile it before retrying.") from None
    if not isinstance(previous, dict):
        raise SetupError("Invalid manifest.json; reconcile it before retrying.")
    trackers, tracker_settings = _tracker_settings(args, previous)
    if getattr(args, "auto_review", None) is None and "auto_review" in previous:
        if not isinstance(previous["auto_review"], str):
            raise SetupError("Invalid auto review in manifest.json; reconcile it before retrying.")
        auto_review = validate("auto_review", previous["auto_review"])
    else:
        auto_review = _field(args, "auto_review", "Automatic PR reviews (all/requested-only): review collaborator PRs only when requested in requested-only mode.", default="all")
    if requested_packages is None:
        packages = previous.get("system_packages", []) if isinstance(previous, dict) else None
        if not isinstance(packages, list):
            raise SetupError("Invalid system packages in manifest.json; reconcile it before retrying.")
        system_packages = validate_system_packages(packages)
    gate = getattr(args, "worklink_gate", None)
    gate = previous.get("worklink_gate", "local") if gate is None else gate
    if not isinstance(gate, str):
        raise SetupError("Invalid worklink gate in manifest.json; reconcile it before retrying.")
    gate = validate("worklink_gate", gate)
    quick_gate_command = getattr(args, "quick_gate_command", None)
    quick_gate_command = previous.get("quick_gate_command") if quick_gate_command is None else quick_gate_command
    if quick_gate_command is not None:
        if not isinstance(quick_gate_command, str):
            raise SetupError("Invalid quick gate command in manifest.json; reconcile it before retrying.")
        quick_gate_command = validate("quick_gate_command", quick_gate_command)
        if gate != "ci":
            raise SetupError("--quick-gate-command is only valid with --worklink-gate ci.")
    if deploy.exists() and (not deploy.is_dir() or deploy.is_symlink()):
        raise SetupError("Deployment directory must be a real directory; choose a new --deploy path.")
    existing_env = (deploy / "compose.env").read_text() if (deploy / "compose.env").is_file() else ""
    prior = dict(line.split("=", 1) for line in existing_env.splitlines() if re.match(r"^[A-Z_][A-Z0-9_]*=", line))
    alert_adapter, alert_channel_id, alert_env = _alert_settings(args, owner, previous, prior)
    print("Operator alerts: " + alert_adapter + (" (" + alert_channel_id + ")" if alert_channel_id else ""))
    token = _secret(args, "github_token", "Bot PAT: create it with the settings above; input is hidden.", previous=prior.get("GITHUB_TOKEN"))
    validate_token(token, bot, repo, request)
    tracker_env: dict[str, str] = {}
    if "linear" in trackers:
        tracker_env["LINEAR_API_KEY"] = _secret(args, "linear_api_key", "Linear access token (input hidden).",
                                                previous=prior.get("LINEAR_API_KEY"))
    if "jira" in trackers:
        tracker_env["JIRA_SITE"] = str(tracker_settings["jira_site"])
        tracker_env["JIRA_EMAIL"] = _secret(args, "jira_email", "Jira service account email (input hidden).",
                                            previous=prior.get("JIRA_EMAIL"))
        tracker_env["JIRA_API_TOKEN"] = _secret(args, "jira_api_token", "Jira service account API token (input hidden).",
                                                previous=prior.get("JIRA_API_TOKEN"))
    spec = _field(args, "model_spec", "Model spec: choose an API-backed openai:model or anthropic:model; used for agent turns.")
    provider = spec.split(":", 1)[0]
    key_name = provider.upper() + "_API_KEY"
    model_key = _secret(args, "model_key", f"Model key: obtain it in your {provider} API console; input is hidden.", previous=prior.get(key_name))
    saga_spec = _field(args, "saga_model_spec",
                        "Saga makes frequent, short, latency-sensitive memory calls; a faster, cheaper model is recommended. "
                        f"Recommended: {provider}:{DEFAULT_SAGA_MODELS[provider]}:{DEFAULT_SAGA_EFFORT}. "
                        f"Your agent's {spec} is also available if you prefer it.",
                        default=provider + ":" + DEFAULT_SAGA_MODELS[provider] + ":" + DEFAULT_SAGA_EFFORT)
    saga_provider = saga_spec.split(":", 1)[0]
    saga_key_env = saga_provider.upper() + "_API_KEY"
    extra_keys = {}
    if saga_key_env != key_name:
        extra_keys[saga_key_env] = _secret(args, "saga_model_key", "Saga model API key (input hidden).", previous=prior.get(saga_key_env))
    embedding = _field(args, "embedding", "Embedding model: local voyage-4-nano runs on CPU without a key "
                       "and shares Voyage 4's embedding space; API providers are also available.", default="local")
    embedding_key_env = EMBEDDING_MODELS[embedding.split(":", 1)[0]][3]
    if embedding_key_env == key_name:
        print(f"Reusing {key_name} for embeddings.")
    elif embedding_key_env and embedding_key_env not in extra_keys:
        extra_keys[embedding_key_env] = _secret(args, "embedding_key", "Embedding API key (input hidden).",
                                                 previous=prior.get(embedding_key_env))
    source_ref = _field(args, "source_ref", "Mimir source ref: a pushed branch or tag on the canonical GitHub repository.", default="refs/heads/feature/plugins")
    source_repo = _field(args, "source_repo", "Mimir source owner/name: canonical repo or a trusted fork.", default=DEFAULT_SOURCE_REPO)
    source_url = "https://github.com/" + source_repo + ".git"
    status, source = request("https://api.github.com/repos/" + source_repo + "/git/ref/" + source_ref[5:], token)
    if status != 200 or not isinstance(source, dict) or not _SHA.fullmatch(str(source.get("object", {}).get("sha", ""))):
        raise SetupError("Cannot verify a pushed full Mimir source SHA from --source-ref; push the ref and retry.")
    sha = source["object"]["sha"]
    files = {name: _content(repo, branch, name, token, request) for name in
             ("rust-toolchain.toml", "Cargo.toml", "package.json", "pyproject.toml", "uv.lock", ".python-version",
              ".github/workflows/rust.yml", ".github/workflows/ci.yml")}
    kind, version, suggestion = detect_toolchain(files)
    suggestion = _verify_script(repo, branch, token, request) or suggestion
    nextest_version = _tool_pin(files, "nextest", DEFAULT_NEXTEST_VERSION)
    uv_version = _tool_pin(files, "uv", DEFAULT_UV_VERSION)
    print(f"Detected {kind} {version}; suggested test command: {suggestion}")
    print("System packages: " + (", ".join(system_packages) if system_packages else "none"))
    test = _field(args, "test_command", "Test command: verify in your repo's CI/docs, then confirm this suggestion or override it.", default=suggestion)
    review_tests = _field(args, "review_tests", "Review tests (targeted/full/none): CI supplies full-suite evidence when targeted or none.", default="targeted")
    previous_port = None
    compose_path = deploy / "compose.yml"
    if compose_path.is_file():
        match = re.search(r'127\.0\.0\.1:(\d+):8080', compose_path.read_text())
        previous_port = match.group(1) if match else None
    port = int(_field(args, "port", "Port: find a free localhost port; never expose this agent directly on the network.", default=previous_port or str(_port())))
    if deploy.exists() and any(deploy.iterdir()) and not compose_path.is_file():
        raise SetupError("Deployment directory is not an existing Cultivate setup; choose an empty --deploy directory.")
    if compose_path.is_file():
        print("Existing deployment found; checking and preserving operator secrets and provisioned state.")
    old_image = _previous_image(deploy, previous) if compose_path.is_file() else None
    if compose_path.is_file() and old_image is None:
        raise SetupError("Cannot identify previous agent image from build.sh; reconcile it before retrying.")
    tag = "mimir-cultivate-root:" + sha
    layer_tag = "mimir-cultivate-agent:" + sha + "-" + kind + version.replace(".", "-")
    extras = {provider, saga_provider, "mcp", "local-embeddings"}
    if alert_adapter in {"discord", "slack"}:
        extras.add(alert_adapter)
    build_args = ["--build-arg MIMIR_GIT_REF=" + source_ref,
                  "--build-arg MIMIR_CONTROLLER_COMMIT=" + sha,
                  "--build-arg MIMIR_EXECUTOR_COMMIT=" + sha,
                  "--build-arg MIMIR_PLUGIN_SELECTION=cultivate",
                  "--build-arg CHAINLINK_ARTIFACT_STAGE=chainlink-build",
                  "--build-arg MIMIR_ENABLE_OPENCODE=1",
                   "--build-arg MIMIR_EXTRAS=" + ",".join(sorted(extras))]
    build_script = ("#!/bin/sh\nset -eu\n"
                    + "docker build -t " + tag + " " + " ".join(build_args) + " " + source_url + "#" + sha + "\n"
                    + "docker build -t " + layer_tag + " -f Dockerfile.toolchain .\n")
    from mimir.scaffold_docker import render_compose_yml

    # Start from the existing loopback-only compose renderer; use root-image
    # service/volumes rather than its pypi installation build path.
    compose = render_compose_yml(service_name="agent", web_port=port, mode="pypi")
    compose = render_compose(compose, layer_tag, kind, repo.lower().split('/')[1])
    generated = {"compose.yml": compose, ".gitignore": "*\n",
                 "provision.py": _PROVISION, "compose.provision.yml": _PROVISION_COMPOSE}
    _check_generated(deploy, generated, previous, old_image, force=bool(getattr(args, "force", False)),
                     source_repo=source_repo, token=token, request=request)
    # Query the old running container before publishing the new Compose image.
    was_running = bool(run(["docker", "compose", "ps", "-q", "agent"], cwd=deploy)) if old_image else False
    if was_running and not getattr(args, "force", False):
        _check_in_flight(deploy, run)
    if old_image and old_image[0] != sha:
        print(f"Upgrading {old_image[0]} -> {sha}")
    deploy.mkdir(parents=True, exist_ok=True)
    deploy.chmod(0o700)
    # The layer and build script are wholly generated from setup inputs.
    _write(deploy / "Dockerfile.toolchain", render_layer(kind, version, tag,
           nextest_version=nextest_version, uv_version=uv_version, system_packages=system_packages), replace=True)
    _write(deploy / "build.sh", build_script, mode=0o755, replace=True)
    for name, text in generated.items():
        _write(deploy / name, text, replace=True)
    # The toolchain layer has no COPY instructions. Docker still sends the
    # context to the daemon: exclude compose.env and the web key by default.
    _write(deploy / ".dockerignore", "*\n!Dockerfile.toolchain\n")
    api_key = _env(deploy, {
        "MIMIR_API_KEY": secrets.token_urlsafe(32), "GITHUB_TOKEN": token, "GH_TOKEN": token,
        "GH_USER_NAME": bot, "GH_USER_EMAIL": bot_email, "MIMIR_GITHUB_SELF_LOGIN": bot,
        "GITHUB_REPOS": repo, "MIMIR_MODEL_SPEC": spec, key_name: model_key,
          "MIMIR_WEB_HOST": "0.0.0.0", **extra_keys, **alert_env, **tracker_env,
    }, manage_alerts=alert_adapter != "none" or "operator_alert_adapter" in previous)
    # Provisioning code is a static, non-secret program read from the selected
    # pinned image; values are passed through compose.env or a non-secret manifest.
    manifest = {"slug": repo.lower(), "branch": branch, "toolchain": kind, "owner": owner, "display": display,
                   "email": owner_email, "test": test, "review_tests": review_tests,
                   "auto_review": auto_review,
                   "worklink_gate": gate, "quick_gate_command": quick_gate_command,
                   "trackers": trackers, "tracker_settings": tracker_settings,
                   "tracker_config": _tracker_config(trackers, tracker_settings),
                   "rotate": bool(args.rotate_web_key),
                  "force": bool(getattr(args, "force", False)),
                  "saga_model_spec": saga_spec, "embedding": embedding, "reembed": bool(args.reembed),
                   "system_packages": system_packages,
                   "operator_alert_adapter": alert_adapter, "operator_alert_channel_id": alert_channel_id,
                   "source_sha": sha, "layer_tag": layer_tag,
                   "generated_sha256": {name: _digest(text) for name, text in generated.items()}}
    _write(deploy / "manifest.json", json.dumps(manifest, indent=2) + "\n", replace=True)
    # The provision script and manifest are mounted read-only for a one-shot
    # non-root container, never into the long-running agent.
    no_start = bool(getattr(args, "no_start", False))
    def docker_step(step: str, argv: list[str]) -> str:
        try:
            return run(argv, cwd=deploy)
        except SetupError as exc:
            if str(exc) in {_PUSH_PROBE_HELP, _PUSH_PROBE_UNREACHABLE_HELP} or str(exc).startswith("Modified Cultivate skill cultivate:"):
                raise
            # Do not interpolate subprocess output (including compose.env values).
            raise SetupError(f"Docker {step} failed; inspect the deployment and docker compose logs.") from None

    print(f"Building pushed Mimir commit {sha} ({source_ref})")
    docker_step("build", ["docker", "build", "-t", tag, "--build-arg", "MIMIR_GIT_REF=" + source_ref,
          "--build-arg", "MIMIR_CONTROLLER_COMMIT=" + sha, "--build-arg", "MIMIR_EXECUTOR_COMMIT=" + sha,
          "--build-arg", "MIMIR_PLUGIN_SELECTION=cultivate",
          "--build-arg", "CHAINLINK_ARTIFACT_STAGE=chainlink-build", "--build-arg", "MIMIR_ENABLE_OPENCODE=1",
           "--build-arg", "MIMIR_EXTRAS=" + ",".join(sorted(extras)), source_url + "#" + sha])
    docker_step("build", ["docker", "build", "-t", layer_tag, "-f", "Dockerfile.toolchain", "."])
    docker_step("init", ["docker", "compose", "run", "--rm", "init"])
    if was_running:
        docker_step("stop", ["docker", "compose", "stop", "agent"])
    try:
        provisioned = docker_step("provision", ["docker", "compose", "-f", "compose.yml", "-f", "compose.provision.yml", "run", "--rm", "--no-deps",
                                           "--user", "1001:1001", "--entrypoint", "python", "agent", "/opt/cultivate-provision.py"])
    except Exception:
        if was_running:
            # Start the existing container, not the newly configured image.
            # A refused upgrade must not leave a healthy deployment stopped.
            try:
                docker_step("start", ["docker", "compose", "start", "agent"])
            except Exception:
                print("Provisioning failed and the previous agent could not be restarted; "
                      "run docker compose start agent from the deployment directory.")
            else:
                print("Provisioning failed; agent restarted on its previous image.")
        raise
    for line in provisioned.splitlines():
        if line.startswith("Upgraded skill cultivate:"):
            print(line)
    if not no_start:
        docker_step("up", ["docker", "compose", "up", "-d", "agent"])
    if old_image and old_image[1] != layer_tag:
        print("Previous agent image retained: " + old_image[1])
        print("Rollback (from " + str(deploy) + "): docker compose -f compose.yml -f <(printf 'services:\\n  agent:\\n    image: "
              + old_image[1] + "\\n') up -d --force-recreate --no-deps agent")
    if no_start:
        print(f"Build and provisioning complete. To start the agent later, from {deploy} run: docker compose up -d agent")
        print("Then re-run mimir cultivate setup with the same deployment and settings, without --no-start, "
              "to retrieve the owner web key and verify the deployment. Live verification has not run.")
        return
    # docker cp writes into a pre-created mode-600 path, without putting key in
    # stdout, argv or the subprocess exception path.
    key_path = deploy / "owner-web-key"
    if key_path.is_symlink() or (key_path.exists() and not key_path.is_file()):
        raise SetupError("Owner web key path must be a regular file; move it aside and retry.")
    if not key_path.exists() or args.rotate_web_key:
        container = run(["docker", "compose", "ps", "-q", "agent"], cwd=deploy)
        if not re.fullmatch(r"[a-f0-9]{12,64}", container):
            raise SetupError("Cannot locate running agent for web-key retrieval.")
        with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as out:
            proc = subprocess.run(["docker", "exec", "-u", "1001", container, "cat", "/home/mimir/agent/owner-web-key"],
                                  stdout=out, stderr=subprocess.DEVNULL, timeout=30, check=False)
        key_path.chmod(0o600)
        if proc.returncode:
            raise SetupError("Could not retrieve owner web key; check container home volume.")
    key_path.chmod(0o600)
    verify(deploy, port, repo, key_path.read_text().strip(), kind, version, system_packages,
           alert_adapter, **({"trackers": tuple(trackers)} if trackers else {}))
    print(f"Cultivate ready: http://127.0.0.1:{port}  Owner web key: {key_path}  Next: use the key to sign in.")
    print("Issues are tracked in Chainlink in the agent home (/home/mimir/agent).")
    print('File a leaf: docker compose exec -w /home/mimir/agent agent chainlink issue create '
          '"<title>" --description "<acceptance and review criteria>" --priority medium')
    print('Arm it: docker compose exec -w /home/mimir/agent agent chainlink issue label <issue-id> worklink:ready')


_PROVISION_COMPOSE = "services:\n  agent:\n    volumes:\n      - ./provision.py:/opt/cultivate-provision.py:ro\n      - ./manifest.json:/opt/cultivate-manifest.json:ro\n"


_PROVISION = '''from pathlib import Path
import json
import os
import re
import subprocess
import uuid
import yaml
from mimir.identities_populator import add_identity_alias, issue_web_key
from mimir.identities import hash_web_key
from mimir.plugins.skills import (resolve_skill, install_skill, skill_state, skill_inventory,
                                  previous_skill_inventories, upgrade_command)
from mimir.skill_install import _file_hashes
from mimir_cultivate.setup import _provision_saga, _provision_worklink

m = json.loads(Path('/opt/cultivate-manifest.json').read_text())
changed = False
home = Path('/home/mimir/agent')
os.environ['HOME'] = '/home/mimir'
os.environ['XDG_CACHE_HOME'] = '/home/mimir/.cache'
changed = _provision_saga(home, m['saga_model_spec'], m['embedding'], m['reembed'])
repo = Path('/workspace') / m['slug'].split('/')[1]
origin = 'https://github.com/' + m['slug'] + '.git'
# Persist only the helper program, never the token. Git runs it for future
# fetch/push too; the credential is read at runtime from the process environment.
helper = '!f() { if [ "$1" = get ]; then printf "%s\\\\n" "username=x-access-token" "password=$GITHUB_TOKEN"; fi; }; f'
git_auth = ['git', '-c', 'credential.helper=', '-c', 'credential.https://github.com.helper=' + helper]
if not (repo / '.git').is_dir():
    subprocess.run(git_auth + ['clone', '--branch', m['branch'], origin, str(repo)],
                   capture_output=True, check=True)
subprocess.run(['git', '-C', str(repo), 'config', '--local', 'credential.helper', ''],
               capture_output=True, check=True)
subprocess.run(['git', '-C', str(repo), 'config', '--local', 'credential.https://github.com.helper', helper],
               capture_output=True, check=True)
result = subprocess.run(['git', '-C', str(repo), 'config', '--local', '--get', 'remote.origin.url'],
                        capture_output=True, text=True, check=True)
if result.stdout.strip() != origin:
    raise RuntimeError('existing checkout origin disagrees with target; refuse to bind')
# Use the checkout's persisted helper, exactly as the agent will. Never print
# git output: remote errors can reflect credentials. A dry run creates no ref.
probe = subprocess.run(['git', '-C', str(repo), 'push', '--dry-run', 'origin',
                        'HEAD:refs/heads/cultivate-setup-probe-' + uuid.uuid4().hex],
                       capture_output=True, check=False)
if probe.returncode:
    # Classify captured bytes locally; never relay git's credential-bearing output.
    auth_failure = re.search(
        rb'The requested URL returned error: (?:401|403)(?![0-9])|'
        rb'Authentication failed|Permission to .+ denied|could not read Username',
        probe.stderr, re.IGNORECASE,
    )
    marker = 'CULTIVATE_PUSH_PROBE_FAILED' if auth_failure else 'CULTIVATE_PUSH_PROBE_UNREACHABLE'
    print(marker, flush=True)
    raise SystemExit(1)
inventory = home / 'repositories.yaml'
config = yaml.safe_load(inventory.read_text()) if inventory.exists() else {}
config = config or {}
base = Path('/workspace/worklink/base') / repo.name
entry = {'slug': m['slug'], 'root': str(repo), 'mode': 'rw', 'origin': origin,
           'base_branch': m['branch'], 'test_command': m['test'], 'review_tests': m['review_tests'],
           'auto_review': m.get('auto_review', 'all'),
           'worklink_gate': m.get('worklink_gate', 'local'), 'quick_gate_command': m.get('quick_gate_command'),
           'worklink_base': str(base)}
rows = config.setdefault('repositories', [])
old = next((r for r in rows if r.get('slug') == m['slug']), None)
# Back-fill only fields absent from an otherwise identical legacy declaration.
# Explicit values (including the gate mode) are never silently replaced.
optional = {'worklink_base', 'review_tests', 'auto_review', 'worklink_gate', 'quick_gate_command'}
if old is not None and set(entry) - set(old) <= optional and all(old[k] == entry[k] for k in old.keys() & entry.keys()):
    missing = {k: v for k, v in entry.items() if k not in old}
    old.update(missing)
    changed = bool(missing) or changed
# Compare and override only setup-owned fields; operator-only policy survives
# both forced reconciliation and subsequent ordinary/idempotent setup runs.
differs = old is not None and any(k not in old or old[k] != v for k, v in entry.items())
if differs and not m.get('force', False):
    raise RuntimeError('existing repository declaration differs; reconcile manually')
if differs:
    old.update(entry)
    changed = True
if old is None:
    rows.append(entry)
    changed = True
roots = config.setdefault('allowed_roots', [])
for path in ('/workspace/pr-leases', '/workspace/worklink'):
    if not any(r.get('root') == path for r in roots):
        roots.append({'root': path, 'mode': 'rw'})
        changed = True
text = yaml.safe_dump(config, sort_keys=False)
if not inventory.exists() or inventory.read_text() != text:
    inventory.write_text(text)
    changed = True
changed = _provision_worklink(home, entry, base=base,
                            checkout=repo, git_auth=git_auth, force=m.get('force', False),
                            toolchain=m.get('toolchain'),
                            tracker=m.get('tracker_config')) or changed
skills = []
for name in ('github-poller', 'github-ci-watch', 'github', 'review') + (('tracker-poller',) if m.get('trackers') else ()):
    skill = resolve_skill('cultivate:' + name, set())
    if skill is None:
        raise RuntimeError('missing optional Cultivate skill: ' + name)
    skills.append((skill, skill_state(home, skill)))
# Refuse before changing any skill, so a modified tree cannot leave a partial upgrade.
for skill, state in skills:
    if state == 'modified' and not m.get('force', False):
        print('CULTIVATE_SKILL_MODIFIED:' + skill.name, flush=True)
        raise RuntimeError('modified Cultivate skill ' + skill.label +
                           '; reconcile it or run ' + upgrade_command(skill) +
                           ' (or rerun setup with --force to back up and restore it)')
# Resolve all display metadata before the first install/restore. A metadata
# error must not interrupt the loop after earlier skills have been replaced.
upgrades = {}
for skill, state in skills:
    if state in ('upgrade available', 'modified'):
        releases = previous_skill_inventories(skill)
        old_release = 'modified'
        if state == 'upgrade available':
            # This walker is only for display, not upgrade authorization. Its
            # exclusions can differ from skill_state's strict tree comparison.
            actual = _file_hashes(home / 'skills' / skill.dirname)
            old_release = next((version for version, files in releases.items()
                                if files == actual), 'recorded release')
        inventory = skill_inventory(skill)
        new_release = next((version for version, files in releases.items()
                            if files == inventory), 'packaged')
        upgrades[skill.name] = (old_release, new_release)
for skill, state in skills:
    if state == 'not installed':
        install_skill(home, skill)
        changed = True
    elif state in ('upgrade available', 'modified'):
        old_release, new_release = upgrades[skill.name]
        install_skill(home, skill, restore=True)
        print('Upgraded skill ' + skill.label + ' (' + old_release + ' -> ' + new_release + ')')
        changed = True
identities = home / 'state' / 'identities.yaml'
doc = yaml.safe_load(identities.read_text()) if identities.exists() else {}
person = next((p for p in (doc or {}).get('people', []) if p.get('canonical') == m['owner']), None)
if person is not None and not {'user', 'admin'}.issubset(set(person.get('access', {}).get('roles', []))):
    raise RuntimeError('owner exists without admin+user roles; reconcile explicitly')
if person is None:
    add_identity_alias(home, m['owner'], 'email:' + m['email'], m['display'])
    changed = True
key_file = home / 'owner-web-key'
if key_file.is_symlink():
    raise RuntimeError('owner web key path must not be a symlink')
if key_file.exists() and not m['rotate']:
    key_file.chmod(0o600)
    if person is None or hash_web_key(key_file.read_text().strip()) not in person.get('aliases', []):
        raise RuntimeError('owner web-key file no longer matches identities; rotate explicitly')
else:
    from mimir.identities_populator import _default_web_key
    import os
    raw = _default_web_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(raw + '\\n')
    issue_web_key(home, m['owner'], roles=['user', 'admin'] if person is None else None,
                  label='cultivate-setup', rotate=m['rotate'], key_factory=lambda: raw)
    changed = True
print('SETUP_CHANGED=' + ('1' if changed else '0'))
'''
