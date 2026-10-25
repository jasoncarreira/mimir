"""Operator-side, provenance-pinned Cultivate Docker installer.

No credential is placed on a command line or in a generated build context.
The Docker and GitHub boundaries are deliberately injectable for offline tests.
"""

from __future__ import annotations

import argparse
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

from .repository import valid_repository_slug


# Tool defaults are explicit; repository declarations take precedence.
DEFAULT_SOURCE_REPO = "jasoncarreira/mimir"
DEFAULT_NEXTEST_VERSION = "0.9.148"
DEFAULT_UV_VERSION = "0.11.0"
DEFAULT_SAGA_MODELS = {"openai": "gpt-5.4-nano", "anthropic": "claude-haiku-4-5"}
LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODELS = {"local": ("onnx", LOCAL_MODEL, 384, None),
                    "openai": ("openai", "text-embedding-3-small", 1536, "OPENAI_API_KEY"),
                    "voyage": ("voyage", "voyage-4-lite", 1024, "VOYAGE_API_KEY")}

PAT_HELP = (
    "Create a fine-grained PAT at https://github.com/settings/personal-access-tokens/new: "
    "resource owner = the bot account; repository access = only the target repository; "
    "Contents, Pull requests, Issues: Read and write; Actions, Checks, Commit statuses, "
    "Metadata: Read-only. Invite the bot as a Write collaborator on the repository "
    "and have the bot accept the invitation before continuing."
)
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_SHA = re.compile(r"[0-9a-f]{40}")


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
    elif name in {"model_spec", "saga_model_spec"}:
        ok = bool(re.fullmatch(r"(openai|anthropic):[A-Za-z0-9_.-]+", value))
    elif name == "embedding":
        ok = value == "local" or bool(re.fullmatch(r"(openai|voyage):[A-Za-z0-9_.-]+", value))
    elif name == "test_command":
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
    sources = [getattr(args, name + "_file"), getattr(args, name + "_stdin")]
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
    """Probe actual capabilities, not untrusted PAT scope headers."""
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
        raise SetupError("Bot lacks push permission; invite it as a Write collaborator and accept the invitation (Contents: Read and write).")
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
                 uv_version: str = DEFAULT_UV_VERSION) -> str:
    """Keep version-pinned tools on /usr/local/bin, without changing shell policy."""
    if kind == "rust":
        install = ("ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/workspace/.cargo\n"
                   "ENV CARGO_INSTALL_ROOT=/opt/cargo\n"
                   "ENV CARGO_HOME=/opt/cargo\n"
                   "RUN arch=$(dpkg --print-architecture); case $arch in amd64) triple=x86_64-unknown-linux-gnu;; "
                   "arm64) triple=aarch64-unknown-linux-gnu;; *) exit 2;; esac && "
                   "mkdir -p /opt/rustup /opt/cargo && "
                   "curl -fsSLo /tmp/rustup-init https://static.rust-lang.org/rustup/archive/1.28.2/$triple/rustup-init && "
                   "chmod 755 /tmp/rustup-init && /tmp/rustup-init -y "
                   f"--no-modify-path --default-toolchain {version} && rm /tmp/rustup-init && "
                   "chmod -R a+rX /opt/rustup /opt/cargo && "
                   "ln -s /opt/cargo/bin/rustup /usr/local/bin/rustup && "
                   "ln -s /opt/cargo/bin/cargo /usr/local/bin/cargo && "
                   "ln -s /opt/cargo/bin/rustc /usr/local/bin/rustc && "
                   "ln -s /opt/cargo/bin/rustfmt /usr/local/bin/rustfmt && "
                   "ln -s /opt/cargo/bin/cargo-clippy /usr/local/bin/cargo-clippy && "
                   "arch=$(dpkg --print-architecture); case $arch in amd64) platform=linux;; arm64) platform=linux-arm;; *) exit 2;; esac && "
                   f"curl -fsSLo /tmp/nextest.tar.gz https://get.nexte.st/{nextest_version}/$platform && "
                   "tar -xzf /tmp/nextest.tar.gz -C /usr/local/bin cargo-nextest && rm /tmp/nextest.tar.gz\n"
                   "ENV CARGO_HOME=/workspace/.cargo\n")
    elif kind == "node":
        install = (f"RUN arch=$(dpkg --print-architecture); case $arch in amd64) arch=x64;; arm64) ;; *) exit 2;; esac && "
                   f"curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v{version}/node-v{version}-linux-$arch.tar.xz "
                   "&& mkdir -p /opt/node && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 "
                   "&& ln -sf /opt/node/bin/node /usr/local/bin/node && ln -sf /opt/node/bin/npm /usr/local/bin/npm "
                   "&& ln -sf /opt/node/bin/npx /usr/local/bin/npx && rm /tmp/node.tar.xz\n")
    else:
        install = ("ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python\n"
                   f"RUN curl -LsSf https://astral.sh/uv/{uv_version}/install.sh | sh && "
                   "cp /root/.local/bin/uv /usr/local/bin/uv && cp /root/.local/bin/uvx /usr/local/bin/uvx\n"
                   f"RUN uv python install {version} && chmod -R a+rX /opt/uv-python\n")
    return f"FROM {root_image}\nUSER root\n{install}RUN mkdir -p /workspace /workspace/pr-leases /workspace/worklink && chown -R 1001:1001 /workspace\nUSER root\n"


def render_compose(template: str, layer_tag: str, kind: str) -> str:
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
                            "MIMIR_HOME": "/home/mimir/agent", "MIMIR_PLUGINS": "cultivate"})
        # Worklink requires its own dedicated base, not the live working checkout.
        environment.pop("WORKLINK_REPO", None)
        volumes = ["home:/home/mimir/agent", "cache:/home/mimir/.cache", "workspace:/workspace",
                   "pr-leases:/workspace/pr-leases", "worklink:/workspace/worklink"]
        agent["volumes"] = volumes
        agent.setdefault("depends_on", {})["init"] = {"condition": "service_completed_successfully"}
        doc["services"]["init"] = {
            "image": layer_tag, "user": "root", "entrypoint": ["/bin/sh", "-c"],
            "command": ["mkdir -p /workspace/pr-leases /workspace/worklink /home/mimir/agent /home/mimir/.cache "
                        "&& chown -R 1001:1001 /workspace /home/mimir/agent /home/mimir/.cache"],
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


def _env(deploy: Path, updates: dict[str, str]) -> str:
    path = deploy / "compose.env"
    existing = path.read_text() if path.exists() else ""
    lines = existing.splitlines()
    current = dict(line.split("=", 1) for line in lines if re.match(r"^[A-Z_][A-Z0-9_]*=", line))
    for key, value in updates.items():
        if key not in current:
            lines.append(f"{key}={value}")
        elif current[key] != value and key != "MIMIR_API_KEY":
            raise SetupError(f"Existing compose.env conflicts on {key}; resolve the operator setting before retrying.")
    _write(path, "\n".join(lines) + "\n", mode=0o600, replace=True)
    return current.get("MIMIR_API_KEY") or updates["MIMIR_API_KEY"]


def _saga_settings(spec: str, embedding: str) -> dict[str, dict[str, object]]:
    provider, model = spec.split(":", 1)
    kind, _, dim, key_env = EMBEDDING_MODELS[embedding.split(":", 1)[0]]
    embed_model = LOCAL_MODEL if embedding == "local" else embedding.split(":", 1)[1]
    llm: dict[str, object] = {"provider": "anthropic" if provider == "anthropic" else "openai_compat",
                              "model": model, "api_key_env": provider.upper() + "_API_KEY"}
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
    from mimir.saga.embeddings import ONNXProvider
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
            prefetch = lambda: ONNXProvider(model_name=LOCAL_MODEL, dimensions=384)._load()
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


def _verify(deploy: Path, port: int, slug: str, owner_key: str) -> None:
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
    rows = json.loads(_run(["docker", "compose", "exec", "-T", "agent", "mimir", "plugins", "list", "--json"], cwd=deploy))
    if not any(row.get("name") == "cultivate" and row.get("enabled") is True for row in rows):
        raise SetupError("mimir plugins list did not show cultivate enabled; check process MIMIR_PLUGINS.")
    # Check current binding, not historical logs (a resolved alert may remain
    # in the log forever after an operator fixes the checkout).
    binding = _run(["docker", "compose", "exec", "-T", "agent", "python", "-c",
                    "import json; from mimir_cultivate.repository import repo_binding_startup_alerts; "
                    "print(json.dumps(repo_binding_startup_alerts()))"], cwd=deploy)
    if json.loads(binding):
        raise SetupError("Repository binding attention required; check checkout origin and repositories.yaml.")
    schedule = _http(url + "/api/v1/scheduler", key=owner_key)
    pollers = {row.get("name") for row in schedule.get("data", {}).get("pollers", [])
               if row.get("next_run_at")}
    for poller in ("github-activity", "github-ci-watch"):
        if poller not in pollers:
            raise SetupError(f"{poller} was not scheduled; inspect poller configuration and restart.")
    # POST only acknowledges enqueue. Confirm the assistant reply in history.
    history_url = url + "/api/v1/chat/history"
    prior = _http(history_url, key=owner_key).get("data", {}).get("messages", [])
    response = _http(url + "/api/v1/chat", key=owner_key, data={"content": "Reply with a short setup canary greeting."})
    if not response.get("ok"):
        raise SetupError("Canary chat was not accepted; check owner web-key roles.")
    for attempt in range(60):
        history = _http(history_url, key=owner_key)
        messages = history.get("data", {}).get("messages", [])
        if any(m.get("role") == "assistant" and m.get("text") for m in messages[len(prior):]):
            return
        time.sleep(2)
    raise SetupError("Canary chat was accepted but no assistant reply arrived; inspect model access and logs.")


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
        ("saga-model-spec", "Fast/cheap memory model (openai:... or anthropic:...)."),
        ("embedding", "local, openai:model, or voyage:model (default: local)."),
        ("deploy", "Absolute deployment directory (default: ./cultivate-agent)."),
        ("port", "Free loopback port (default: next free from 8090)."),
        ("test-command", "Verification command for target repo; default detected from repo."),
        ("source-ref", "Pushed Mimir source ref (default: refs/heads/feature/plugins)."),
        ("source-repo", "Mimir source owner/name (default: " + DEFAULT_SOURCE_REPO + ")."),
    ):
        setup.add_argument("--" + name, help=help_text)
    for name in ("github-token", "model-key", "saga-model-key", "embedding-key"):
        setup.add_argument("--" + name + "-file", help="Read secret from a mode-600 file (never from argv).")
        setup.add_argument("--" + name + "-stdin", action="store_true", help="Read one secret line from stdin.")
    setup.add_argument("--accept-license", action="store_true", help="Explicitly accept the shipped Cultivate BSL 1.1 license for unattended setup.")
    setup.add_argument("--rotate-web-key", action="store_true", help="Replace the owner's existing web key intentionally.")
    setup.add_argument("--reembed", action="store_true", help="Re-embed existing Saga vectors offline when changing embeddings.")
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
    if deploy.exists() and (not deploy.is_dir() or deploy.is_symlink()):
        raise SetupError("Deployment directory must be a real directory; choose a new --deploy path.")
    existing_env = (deploy / "compose.env").read_text() if (deploy / "compose.env").is_file() else ""
    prior = dict(line.split("=", 1) for line in existing_env.splitlines() if re.match(r"^[A-Z_][A-Z0-9_]*=", line))
    token = _secret(args, "github_token", "Bot PAT: create it with the settings above; input is hidden.", previous=prior.get("GITHUB_TOKEN"))
    validate_token(token, bot, repo, request)
    spec = _field(args, "model_spec", "Model spec: choose an API-backed openai:model or anthropic:model; used for agent turns.")
    provider = spec.split(":", 1)[0]
    key_name = provider.upper() + "_API_KEY"
    model_key = _secret(args, "model_key", f"Model key: obtain it in your {provider} API console; input is hidden.", previous=prior.get(key_name))
    saga_spec = _field(args, "saga_model_spec",
                       "Saga makes frequent, short, latency-sensitive memory calls; a faster, cheaper model is recommended. "
                       f"Your agent's {spec} is also available if you prefer it.",
                       default=provider + ":" + DEFAULT_SAGA_MODELS[provider])
    saga_provider = saga_spec.split(":", 1)[0]
    saga_key_env = saga_provider.upper() + "_API_KEY"
    extra_keys = {}
    if saga_key_env != key_name:
        extra_keys[saga_key_env] = _secret(args, "saga_model_key", "Saga model API key (input hidden).", previous=prior.get(saga_key_env))
    embedding = _field(args, "embedding", "Embedding model: local runs on CPU in the container without a key; "
                       "API providers can give better retrieval quality.", default="local")
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
    test = _field(args, "test_command", "Test command: verify in your repo's CI/docs, then confirm this suggestion or override it.", default=suggestion)
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
    deploy.mkdir(parents=True, exist_ok=True)
    deploy.chmod(0o700)
    tag = "mimir-cultivate-root:" + sha
    layer_tag = "mimir-cultivate-agent:" + sha + "-" + kind + version.replace(".", "-")
    _write(deploy / "Dockerfile.toolchain", render_layer(kind, version, tag,
           nextest_version=nextest_version, uv_version=uv_version))
    build_args = ["--build-arg MIMIR_GIT_REF=" + source_ref,
                  "--build-arg MIMIR_CONTROLLER_COMMIT=" + sha,
                  "--build-arg MIMIR_EXECUTOR_COMMIT=" + sha,
                  "--build-arg MIMIR_PLUGIN_SELECTION=cultivate",
                  "--build-arg CHAINLINK_ARTIFACT_STAGE=chainlink-build",
                  "--build-arg MIMIR_ENABLE_OPENCODE=1",
                   "--build-arg MIMIR_EXTRAS=" + ",".join(sorted({provider, saga_provider}))]
    _write(deploy / "build.sh", "#!/bin/sh\nset -eu\n"
           + "docker build -t " + tag + " " + " ".join(build_args) + " " + source_url + "#" + sha + "\n"
           + "docker build -t " + layer_tag + " -f Dockerfile.toolchain .\n", mode=0o755)
    from mimir.scaffold_docker import render_compose_yml

    # Start from the existing loopback-only compose renderer; use root-image
    # service/volumes rather than its pypi installation build path.
    compose = render_compose_yml(service_name="agent", web_port=port, mode="pypi")
    compose = render_compose(compose, layer_tag, kind)
    _write(compose_path, compose)
    _write(deploy / ".gitignore", "*\n")
    # The toolchain layer has no COPY instructions. Docker still sends the
    # context to the daemon: exclude compose.env and the web key by default.
    _write(deploy / ".dockerignore", "*\n!Dockerfile.toolchain\n")
    api_key = _env(deploy, {
        "MIMIR_API_KEY": secrets.token_urlsafe(32), "GITHUB_TOKEN": token, "GH_TOKEN": token,
        "GH_USER_NAME": bot, "GH_USER_EMAIL": bot_email, "MIMIR_GITHUB_SELF_LOGIN": bot,
        "GITHUB_REPOS": repo, "MIMIR_MODEL_SPEC": spec, key_name: model_key,
         "MIMIR_WEB_HOST": "0.0.0.0", **extra_keys,
    })
    # Provisioning code is a static, non-secret program read from the selected
    # pinned image; values are passed through compose.env or a non-secret manifest.
    manifest = {"slug": repo.lower(), "branch": branch, "owner": owner, "display": display,
                 "email": owner_email, "test": test, "rotate": bool(args.rotate_web_key),
                 "saga_model_spec": saga_spec, "embedding": embedding, "reembed": bool(args.reembed)}
    _write(deploy / "manifest.json", json.dumps(manifest, indent=2) + "\n", replace=True)
    _write(deploy / "provision.py", _PROVISION)
    # The provision script and manifest are mounted read-only for a one-shot
    # non-root container, never into the long-running agent.
    _write(deploy / "compose.provision.yml", "services:\n  agent:\n    volumes:\n      - ./provision.py:/opt/cultivate-provision.py:ro\n      - ./manifest.json:/opt/cultivate-manifest.json:ro\n")
    was_running = bool(run(["docker", "compose", "ps", "-q", "agent"], cwd=deploy))
    if args.reembed and was_running:
        run(["docker", "compose", "stop", "agent"], cwd=deploy)
    print(f"Building pushed Mimir commit {sha} ({source_ref})")
    run(["docker", "build", "-t", tag, "--build-arg", "MIMIR_GIT_REF=" + source_ref,
         "--build-arg", "MIMIR_CONTROLLER_COMMIT=" + sha, "--build-arg", "MIMIR_EXECUTOR_COMMIT=" + sha,
         "--build-arg", "MIMIR_PLUGIN_SELECTION=cultivate",
         "--build-arg", "CHAINLINK_ARTIFACT_STAGE=chainlink-build", "--build-arg", "MIMIR_ENABLE_OPENCODE=1",
          "--build-arg", "MIMIR_EXTRAS=" + ",".join(sorted({provider, saga_provider})), source_url + "#" + sha], cwd=deploy)
    run(["docker", "build", "-t", layer_tag, "-f", "Dockerfile.toolchain", "."], cwd=deploy)
    run(["docker", "compose", "run", "--rm", "init"], cwd=deploy)
    provisioned = run(["docker", "compose", "-f", "compose.yml", "-f", "compose.provision.yml", "run", "--rm", "--no-deps",
                       "--user", "1001:1001", "--entrypoint", "python", "agent", "/opt/cultivate-provision.py"], cwd=deploy)
    run(["docker", "compose", "up", "-d", "agent"], cwd=deploy)
    if was_running and "SETUP_CHANGED=1" in provisioned.splitlines():
        run(["docker", "compose", "restart", "agent"], cwd=deploy)
        print("Restarted the existing agent once to load provisioned repositories, identities and pollers.")
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
    verify(deploy, port, repo, key_path.read_text().strip())
    print(f"Cultivate ready: http://127.0.0.1:{port}  Owner web key: {key_path}  Next: use the key to sign in.")


_PROVISION = '''from pathlib import Path
import json
import os
import subprocess
import yaml
from mimir.identities_populator import add_identity_alias, issue_web_key
from mimir.identities import hash_web_key
from mimir.plugins.skills import resolve_skill, install_skill
from mimir_cultivate.setup import _provision_saga

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
inventory = home / 'repositories.yaml'
config = yaml.safe_load(inventory.read_text()) if inventory.exists() else {}
config = config or {}
entry = {'slug': m['slug'], 'root': str(repo), 'mode': 'rw', 'origin': origin,
         'base_branch': m['branch'], 'test_command': m['test']}
rows = config.setdefault('repositories', [])
old = next((r for r in rows if r.get('slug') == m['slug']), None)
if old is not None and old != entry:
    raise RuntimeError('existing repository declaration differs; reconcile manually')
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
for name in ('github-poller', 'github-ci-watch', 'github', 'review'):
    skill = resolve_skill('cultivate:' + name, set())
    if skill is None:
        raise RuntimeError('missing optional Cultivate skill: ' + name)
    if not (home / 'skills' / skill.dirname).exists():
        install_skill(home, skill)
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
