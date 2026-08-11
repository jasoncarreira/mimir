from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from .contained_execution import SensitiveMaterialScrubber

__all__ = (
    "OpenCodeProposal",
    "ProposalBuildResult",
    "SpawnTerminalResult",
    "build_opencode_proposal",
    "classify_spawn_terminal_state",
    "cleanup_failure_event",
    "prompt_contains_sensitive_source_path",
    "resolve_artifact_handle",
    "write_spawn_artifacts",
)

MAX_PROPOSAL_PATHS = 1_000
MAX_PROPOSAL_PATH_BYTES = 4_096
MAX_PROPOSAL_NAME_STREAM_BYTES = 4_097_000
MAX_PROPOSAL_PATCH_BYTES = 1_048_576
_SAFE_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PATH_TOKEN = re.compile(r"(?:file://)?/[^\s\"'<>]+")


@dataclass(frozen=True)
class OpenCodeProposal:
    schema_version: int
    kind: str
    base_tree: str
    encoding: str
    byte_length: int
    sha256: str
    patch: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposalBuildResult:
    proposal: OpenCodeProposal | None
    reason_code: str


@dataclass(frozen=True)
class SpawnTerminalResult:
    status: str
    exit_code: int | None
    reason_code: str
    event_type: str
    proposal: OpenCodeProposal | None = None

    def event(self, run_id: str) -> dict[str, str]:
        return {
            "type": self.event_type,
            "run_id": run_id,
            "status": self.status,
            "reason_code": self.reason_code,
        }


def _path_forms(path: Path | str) -> set[str]:
    lexical = os.path.expanduser(os.fspath(path))
    resolved = str(Path(lexical).resolve(strict=False))
    forms = {lexical, resolved}
    for value in tuple(forms):
        candidate = Path(value)
        if candidate.is_absolute():
            forms.add(candidate.as_uri())
    for value in tuple(forms):
        encoded = json.dumps(value, ensure_ascii=False)
        forms.add(encoded)
        forms.add(encoded[1:-1])
    return {value for value in forms if value}


def _beneath(candidate: str, home: Path) -> bool:
    try:
        lexical = Path(os.path.normpath(candidate))
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    for value in (lexical, resolved):
        try:
            value.relative_to(home)
            return True
        except ValueError:
            pass
    return False


def prompt_contains_sensitive_source_path(
    prompt: str,
    *,
    agent_home: Path | str,
    source_paths: Sequence[Path | str] = (),
) -> str | None:
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    home = Path(os.path.expanduser(os.fspath(agent_home))).resolve(strict=False)
    if any(form in prompt for form in _path_forms(agent_home)):
        return "agent_home_path"
    for match in _PATH_TOKEN.finditer(prompt):
        token = match.group(0).rstrip(".,;:!?)]}")
        if token.startswith("file://"):
            token = token[7:]
        if _beneath(token, home):
            return "agent_home_path"
    tokens = {
        match.group(0).rstrip(".,;:!?)]}")
        for match in _PATH_TOKEN.finditer(prompt)
    }
    for source_path in source_paths:
        forms = _path_forms(source_path)
        if tokens.intersection(forms):
            return "config_auth_source_path"
        for form in forms:
            if form.startswith('"') and form.endswith('"') and form in prompt:
                return "config_auth_source_path"
    return None


def _run_git(checkout: Path, *args: str, limit: int | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-C", os.fspath(checkout), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise OSError("proposal Git operation failed")
    if limit is not None:
        return completed.stdout[:limit]
    return completed.stdout


def build_opencode_proposal(
    checkout: Path | str,
    *,
    scrubber: SensitiveMaterialScrubber,
    base_tree: str | None = None,
) -> ProposalBuildResult:
    root = Path(checkout)
    try:
        _run_git(root, "add", "-A")
        name_stream = _run_git(
            root,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-ext-diff",
            limit=MAX_PROPOSAL_NAME_STREAM_BYTES + 1,
        )
        if len(name_stream) > MAX_PROPOSAL_NAME_STREAM_BYTES:
            return ProposalBuildResult(None, "path_bytes")
        if not name_stream:
            return ProposalBuildResult(None, "no_changes")
        paths = name_stream[:-1].split(b"\0") if name_stream.endswith(b"\0") else []
        if not paths or any(not path for path in paths):
            return ProposalBuildResult(None, "proposal_unavailable")
        if len(paths) > MAX_PROPOSAL_PATHS:
            return ProposalBuildResult(None, "path_count")
        for path in paths:
            if len(path) > MAX_PROPOSAL_PATH_BYTES:
                return ProposalBuildResult(None, "path_bytes")
            decoded = os.fsdecode(path)
            pure = PurePosixPath(decoded)
            if pure.is_absolute() or ".." in pure.parts:
                return ProposalBuildResult(None, "proposal_unavailable")
        patch = _run_git(
            root,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            limit=MAX_PROPOSAL_PATCH_BYTES + 1,
        )
        if len(patch) > MAX_PROPOSAL_PATCH_BYTES:
            return ProposalBuildResult(None, "patch_bytes")
        if scrubber.contains_sensitive(patch):
            return ProposalBuildResult(None, "proposal_sensitive_content")
        tree = base_tree or _run_git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tree) is None:
            return ProposalBuildResult(None, "proposal_unavailable")
    except (OSError, UnicodeError, ValueError):
        return ProposalBuildResult(None, "proposal_unavailable")
    proposal = OpenCodeProposal(
        schema_version=1,
        kind="git_binary_patch",
        base_tree=tree.lower(),
        encoding="base64",
        byte_length=len(patch),
        sha256=hashlib.sha256(patch).hexdigest(),
        patch=base64.b64encode(patch).decode("ascii"),
    )
    return ProposalBuildResult(proposal, "proposal_created")


def resolve_artifact_handle(artifact_root: Path | str, handle: str) -> Path:
    if not isinstance(handle, str) or _SAFE_HANDLE.fullmatch(handle) is None:
        raise ValueError("unsafe artifact handle")
    if handle in {".", ".."}:
        raise ValueError("unsafe artifact handle")
    root = Path(artifact_root).resolve(strict=False)
    candidate = (root / handle).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("unsafe artifact handle") from exc
    return candidate


def write_spawn_artifacts(
    artifact_root: Path | str,
    handle: str,
    *,
    prompt: str,
    stdout: bytes | str,
    stderr: bytes | str,
    manifest: Mapping[str, Any],
    scrubber: SensitiveMaterialScrubber,
    proposal: OpenCodeProposal | None = None,
) -> str:
    directory = resolve_artifact_handle(artifact_root, handle)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    prompt_text = scrubber.scrub_text(prompt)
    stdout_text = scrubber.scrub_text(stdout)
    stderr_text = scrubber.scrub_text(stderr)
    scrubbed_manifest = json.loads(scrubber.scrub_text(json.dumps(dict(manifest), ensure_ascii=False)))
    (directory / "prompt.md").write_text(prompt_text, encoding="utf-8")
    (directory / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    (directory / "stderr.txt").write_text(stderr_text, encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(scrubbed_manifest, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if proposal is not None:
        raw_patch = base64.b64decode(proposal.patch, validate=True)
        if scrubber.contains_sensitive(raw_patch):
            raise ValueError("proposal contains sensitive material")
        (directory / "proposal.json").write_text(
            json.dumps(proposal.as_dict(), ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return handle


def classify_spawn_terminal_state(
    *,
    artifact_available: bool = True,
    configuration_reason: str | None = None,
    authentication_reason: str | None = None,
    prompt_reason: str | None = None,
    provisioning_reason: str | None = None,
    containment_available: bool = True,
    timed_out: bool = False,
    output_overflow: bool = False,
    exit_code: int | None = None,
    worker_authentication_required: bool = False,
    proposal_result: ProposalBuildResult | None = None,
) -> SpawnTerminalResult:
    if not artifact_available:
        return SpawnTerminalResult("artifact_unavailable", None, "artifact_unavailable", "spawn_open_code_provisioning_refused")
    if configuration_reason is not None:
        return SpawnTerminalResult("configuration_refused", None, configuration_reason, "spawn_open_code_configuration_refused")
    if authentication_reason is not None:
        return SpawnTerminalResult("authentication_refused", None, authentication_reason, "spawn_open_code_authentication_refused")
    if prompt_reason is not None:
        return SpawnTerminalResult("prompt_refused", None, prompt_reason, "spawn_open_code_prompt_refused")
    if provisioning_reason is not None:
        status_by_reason = {
            "seed_credentials": "seed_credentials_refused",
            "invalid_seed": "seed_refused",
            "unsafe_seed_entry": "seed_refused",
            "seed_changed": "seed_refused",
            "provisioning_unavailable": "provisioning_unavailable",
        }
        status = status_by_reason.get(provisioning_reason, "provisioning_unavailable")
        return SpawnTerminalResult(status, None, provisioning_reason, "spawn_open_code_provisioning_refused")
    if not containment_available:
        return SpawnTerminalResult("containment_unavailable", None, "containment_unavailable", "spawn_open_code_containment_refused")
    if timed_out:
        return SpawnTerminalResult("timeout", None, "timeout", "spawn_open_code_completed")
    if output_overflow:
        return SpawnTerminalResult("output_overflow", exit_code, "output_overflow", "spawn_open_code_completed")
    if exit_code != 0:
        if worker_authentication_required:
            return SpawnTerminalResult("authentication_required", exit_code, "worker_authentication_required", "spawn_open_code_completed")
        return SpawnTerminalResult("failed", exit_code, "worker_nonzero", "spawn_open_code_completed")
    if proposal_result is None:
        return SpawnTerminalResult("proposal_unavailable", 0, "proposal_unavailable", "spawn_open_code_completed")
    reason = proposal_result.reason_code
    if reason == "no_changes":
        return SpawnTerminalResult("succeeded", 0, reason, "spawn_open_code_completed")
    if reason in {"path_count", "path_bytes", "patch_bytes"}:
        return SpawnTerminalResult("proposal_overflow", 0, reason, "spawn_open_code_completed")
    if reason == "proposal_sensitive_content":
        return SpawnTerminalResult(reason, 0, reason, "spawn_open_code_completed")
    if reason != "proposal_created" or proposal_result.proposal is None:
        return SpawnTerminalResult("proposal_unavailable", 0, "proposal_unavailable", "spawn_open_code_completed")
    return SpawnTerminalResult("succeeded", 0, reason, "spawn_open_code_completed", proposal_result.proposal)


def cleanup_failure_event(run_id: str) -> dict[str, str]:
    return {
        "type": "spawn_open_code_cleanup_failed",
        "run_id": run_id,
        "reason_code": "cleanup_failed",
    }
