from __future__ import annotations

import hashlib
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import validator_for


ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs/acp.md").read_text()
EXPECTED_MANIFEST = {
    "id": "mimir",
    "name": "Mimir",
    "version": "0.9.0",
    "description": "Memory-centric AI agent accessible through the Agent Client Protocol.",
    "repository": "https://github.com/jasoncarreira/mimir",
    "authors": ["Jason Carreira"],
    "license": "MIT",
    "distribution": {"uvx": {"package": "mimir-agent==0.9.0", "args": ["acp"]}},
}
EXPECTED_ICON = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 13V3l5 5 5-5v10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>\n'
PROFILE_COMMANDS = [
    "mimir acp profile add-local PROFILE --home /absolute/server/mimir-home",
    "mimir acp profile add-ssh PROFILE --home /absolute/server/mimir-home \\",
    "  --ssh-host host.example --ssh-user mimir --ssh-port 22 \\",
    "  --identity-file /absolute/id_ed25519 --known-hosts-file /absolute/known_hosts",
    "mimir acp profile list",
    "mimir acp profile remove PROFILE",
    "mimir acp credential add PROFILE",
    "mimir acp credential replace PROFILE",
    "mimir acp credential remove PROFILE",
    "mimir acp credential list",
    "mimir acp --profile PROFILE",
]


def section(start: str, end: str) -> str:
    return DOCS.split(start, 1)[1].split(end, 1)[0]


def fenced(language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)\n```", DOCS, re.DOTALL)


def test_manifest_exact() -> None:
    path = ROOT / "registry/mimir/agent.json"
    manifest = json.loads(path.read_bytes())
    assert manifest == EXPECTED_MANIFEST
    assert path.parent.name == manifest["id"]
    assert "authMethods" not in manifest
    assert f"uvx {manifest['distribution']['uvx']['package']} {' '.join(manifest['distribution']['uvx']['args'])}" == "uvx mimir-agent==0.9.0 acp"


def test_schema_digest_then_validation() -> None:
    schema_bytes = (ROOT / "registry/schema/agent.schema.json").read_bytes()
    assert hashlib.sha256(schema_bytes).hexdigest() == "2489443bf21d8211bcd046edeff5931eecca1ee219ffae4efde09e367c5b1236"
    schema = json.loads(schema_bytes)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(schema, format_checker=FormatChecker()).validate(EXPECTED_MANIFEST)


def test_icon_exact() -> None:
    data = (ROOT / "registry/mimir/icon.svg").read_bytes()
    assert data == EXPECTED_ICON
    root = ET.fromstring(data)
    assert root.attrib["width"] == root.attrib["height"] == "16"
    assert root.attrib["viewBox"] == "0 0 16 16"
    assert root.find("{http://www.w3.org/2000/svg}path").attrib["stroke"] == "currentColor"


def test_metadata_lock_and_launch_shape() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert project["project"]["version"] == "0.9.0"
    assert project["project"]["optional-dependencies"]["dev"].count("jsonschema==4.26.0") == 1
    assert project["dependency-groups"]["dev"].count("jsonschema==4.26.0") == 1
    assert "jsonschema==4.26.0" not in project["project"]["dependencies"]
    assert project["project"]["scripts"]["mimir-agent"] == "mimir.entrypoint:main"
    packaging_proof = (ROOT / "tests/test_acp_packaging.py").read_text()
    installed_proof = (ROOT / ".github/assert_installed_acp.py").read_text()
    assert '"mimir-agent": "mimir.entrypoint:main"' in packaging_proof
    assert '"mimir-agent": "mimir.entrypoint:main"' in installed_proof
    root = next(package for package in lock["package"] if package["name"] == "mimir-agent")
    assert root["version"] == "0.9.0"
    assert {item["name"]: item.get("specifier") for item in root["metadata"]["requires-dev"]["dev"]}["jsonschema"] == "==4.26.0"
    pinned_extra = [item for item in root["metadata"]["requires-dist"] if item["name"] == "jsonschema"]
    assert pinned_extra == [{"name": "jsonschema", "marker": "extra == 'dev'", "specifier": "==4.26.0"}]


def test_topology_daemon_contract() -> None:
    for text in [
        "stock ACP client",
        "local credential-aware `mimir acp` stdio proxy",
        "public-key-authenticated SSH relay",
        "owner-only daemon socket",
        "One already-running `mimir run` daemon owns the brain",
        "The proxy never creates a standalone runtime.",
        "`MIMIR_ACP_ENABLED` is unset or true",
        "`$MIMIR_HOME/.mimir/acp/daemon.sock`",
        "mode `0700`",
        "mode `0600`",
        "UTF-8 JSONL ACP frames only",
        "diagnostics go to stderr",
    ]:
        assert text in DOCS


def test_timing_contract() -> None:
    for text in ["bounded to 5 seconds", "bounded to 12 seconds", "no duration limit", "2, 1, and 1 seconds", "waits 1 second for SSH", "terminates and waits 2 seconds", "kills and waits 1 second"]:
        assert text in DOCS


def test_profile_commands_exact() -> None:
    block = fenced("sh")[0]
    assert block.splitlines() == PROFILE_COMMANDS
    assert "MIMIR_ACP_PROFILE" in section("## Profiles and credentials", "## SSH transport")
    assert not {"--destination", "--remote-home", "--identity"} & set(re.findall(r"--[a-z-]+", block))


def test_credentials_auth_and_rotation() -> None:
    text = section("## Profiles and credentials", "## SSH transport")
    for value in [
        "mimir identities issue-key --home /absolute/server/mimir-home CANONICAL --admin",
        "without echo",
        "service `mimir.acp`",
        "server stores only its hash",
        "no plaintext or third-party fallback",
        "fails if no secure backend exists",
        "`MIMIR_API_KEY` supplies transport/route authority and is not the ACP principal key",
        "only `methodId`",
        "protected upstream authenticate request",
        "non-service admin identity",
        "There is no `credential validate` command or pre-activation validation protocol.",
        "mimir identities issue-key --home /absolute/server/mimir-home CANONICAL --rotate-only",
        "immediately invalidates the old key",
        "mimir acp credential replace PROFILE",
        "expected outage between steps 1 and 2",
        "no rollback to the old key",
        "mimir identities revoke-key --home /absolute/server/mimir-home CANONICAL",
        "mimir acp credential remove PROFILE",
    ]:
        assert value in text


def test_ssh_policy_and_trust_boundary() -> None:
    text = section("## SSH transport", "## Stock clients")
    for value in [
        "two independent proofs",
        "SSH public key or certificate",
        "`ssh -T`",
        "batch mode",
        "strict host-key verification",
        "no forwarding, SSH agent, or TTY",
        "mode `0600`",
        'restrict,command="mimir-agent acp relay --home /absolute/server/mimir-home"',
        "optional defense in depth, not required product behavior",
        "MOTD, banner, or shell rc output",
        "client account and proxy",
        "relay/daemon UID",
        "root are trusted with ACP plaintext",
        "Socket modes do not isolate",
    ]:
        assert value in text
    assert all("StrictHostKeyChecking=no" not in block for language in ("sh", "json", "text") for block in fenced(language))
    assert "Never use `StrictHostKeyChecking=no`" in text


def test_stock_client_examples_exact() -> None:
    objects = [json.loads(value) for value in fenced("json")]
    assert objects == [
        {"default_mcp_settings": {"use_idea_mcp": False, "use_custom_mcp": False}, "agent_servers": {"mimir": {"command": "/absolute/path/to/uvx", "args": ["mimir-agent==0.9.0", "acp"], "env": {"MIMIR_ACP_PROFILE": "PROFILE"}}}},
        {"agent_servers": {"mimir": {"type": "custom", "command": "uvx", "args": ["mimir-agent==0.9.0", "acp"], "env": {"MIMIR_ACP_PROFILE": "PROFILE"}}}},
        {"acp.agents": {"mimir": {"command": "uvx", "args": ["mimir-agent==0.9.0", "acp"], "env": {"MIMIR_ACP_PROFILE": "PROFILE"}}}},
    ]
    text = section("## Stock clients", "## Connections")
    for value in [
        "macOS and Linux",
        "Windows client support is deferred",
        "`~/.jetbrains/acp.json`",
        "display/id is `mimir`",
        "ordinary IntelliJ MCP servers are not compatible with Mimir Hands",
        "`formulahendry.acp-client` version `0.2.0`",
        "`e7371659e3ac100db842b419b1361205a193032e`",
        "Microsoft's native VS Code agent system uses AHP",
        "measured 2026-08-09",
    ]:
        assert value in text


def test_connection_session_replay_and_cancellation() -> None:
    text = section("## Connections, sessions, and replay", "## Providers")
    for value in [
        "one active ACP connection per `MIMIR_HOME`",
        "Only a newly authenticated connection",
        "failed or partial authentication cannot evict",
        "owner-bound UUIDv4",
        "`session/load`",
        "identities are fresh",
        "default seven-day TTL",
        "64 MiB limit",
        "revalidates the provider",
        "every durably prepared `session/update`",
        "original sequence",
        "including records already sent",
        "tolerate duplicates",
        "never re-executes effects",
        "Pending requests and frames are not replayed",
        "does not roll back completed effects",
        "cancels and quarantines only that ACP generation",
        "web UI, bridges, scheduler, unrelated work",
    ]:
        assert value in text


def test_hands_and_filesystem_contract() -> None:
    text = section("## Providers, permissions, and filesystems", "## Troubleshooting")
    for value in [
        "providerless by default",
        "one MCP-over-ACP declaration named `mimir-hands`",
        "profile `mimir.hands.v1`",
        "`read`, `edit`, and `shell`",
        "session new, session load, and provider-list change",
        "one-shot `allow_once` or `reject_once`",
        "Arbitrary or multiple providers are rejected",
        "integrated MCP server is not Mimir Hands",
        "Native Mimir tools operate on the daemon host",
        "Mimir Hands operates on the client host",
        "opaque `client-file:` resources",
        "`cwd` is context, not filesystem confinement or a path sandbox",
        "`fs` and `terminal` capabilities but never calls them",
        "`additionalDirectories`",
    ]:
        assert value in text


def test_troubleshooting_contract() -> None:
    text = section("## Troubleshooting", "this heading is absent")
    for value in [
        "`error: connection-failed`",
        "Confirm the selected profile",
        "`mimir run` is running",
        "`MIMIR_ACP_ENABLED` unset or true",
        "`<MIMIR_HOME>/.mimir/acp`",
        "mode `0700`",
        "`daemon.sock` must be mode `0600`",
        "Start or restart `mimir run`",
        "remote `mimir-agent` version is 0.9.0",
        "noninteractive PATH",
        "host-key entry matches",
        "banner-free",
    ]:
        assert value in text


def test_examples_do_not_leak_secrets_or_invent_commands() -> None:
    examples = "\n".join(block for language in ("sh", "json", "text") for block in fenced(language))
    for forbidden in ["MIMIR_API_KEY", "--destination", "--remote-home", "sshpass", "credential validate", "StrictHostKeyChecking=no", "idea_mcp_allowed_tools"]:
        assert forbidden not in examples
    assert "launches a standalone runtime" not in DOCS
    assert "proxy never creates a standalone runtime" in DOCS
