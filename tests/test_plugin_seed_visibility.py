"""Seeded Cultivate jobs and skills stay withheld once Cultivate is deselected.

Each phase runs in a fresh interpreter, so the "provider never imported" claim
covers the whole process and the seed phase leaves no provider state behind.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

#: Every skill Cultivate's defaults seed into ``<home>/skills``.
_SEEDED_SKILLS = (
    "cultivate-chainlink-orchestrator",
    "cultivate-github",
    "cultivate-github-ci-watch",
    "cultivate-github-poller",
    "cultivate-review",
    "cultivate-worklink-tool-pins",
)

_SCRIPT = r'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace

mode, home = sys.argv[1], Path(sys.argv[2])
seeded = json.loads(sys.argv[3])
from mimir.plugins.manager import load_plugins

if mode == "seed":
    from mimir.plugins.defaults import seed_plugin_defaults
    from dataclasses import replace
    manager = load_plugins(enable="cultivate")
    candidate, = manager.policy.selected_plugin_candidates
    # Reproduce a home seeded by the previous manifest (all six defaults).
    old = replace(candidate, manifest=replace(candidate.manifest, defaults={
        **candidate.manifest.defaults,
        "skills": [*candidate.manifest.defaults["skills"],
                   *(path for path, _ in candidate.manifest.optional_skills)],
    }, optional_skills=()))
    seed_plugin_defaults(home, selected=(old,))
    for skill in seeded:
        assert (home / "skills" / skill / "SKILL.md").is_file(), skill
    sys.exit(0)

selected = mode == "selected"
manager = load_plugins(enable="cultivate" if selected else "")
policy = manager.policy

# --- scheduler jobs: the sync and async reload paths the server uses ----------
from mimir.scheduler import Scheduler

async def enqueue(event):
    return True

async def callback():
    return None

def callback_hour(scheduler):
    trigger = scheduler._scheduler.get_job("operator-cb").trigger
    return str(next(field for field in trigger.fields if field.name == "hour"))

# A seeded entry may also retime a registered callable; it must not while deselected.
seeded_hour = "3" if selected else "4"
for reload in ("sync", "async"):
    scheduler = Scheduler(home / "scheduler.yaml", enqueue, home=home, policy=policy)
    assert scheduler.register_callable("operator-cb", callback, "0 4 * * *", job_id="operator-cb")
    assert callback_hour(scheduler) == seeded_hour, reload
    if reload == "sync":
        scheduler.reload()
    else:
        asyncio.run(scheduler._reload_async())
    ids = {job.id for job in scheduler._scheduler.get_jobs()}
    assert "scheduler:operator-daily" in ids, (reload, ids)
    assert ("scheduler:cultivate-nightly" in ids) is selected, (reload, ids)
    assert callback_hour(scheduler) == seeded_hour, reload

# --- the agent's skills catalog (deepagents' skills slot) ---------------------
import mimir.agent  # noqa: F401 -- the agent's deepagents patches, as in production
from deepagents import create_deep_agent
from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from mimir.readonly_backend import MimirFilesystemMiddleware, WriteGuardBackend
from mimir.skill_visibility import (
    SeedFilteredSkillsMiddleware, hidden_home_skill_dirs, visible_skill_names,
)

backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
sources = [str(home / "skills")]
middleware = SeedFilteredSkillsMiddleware(backend=backend, sources=sources, home=home, policy=policy)
expected = {"operator-skill", *(seeded if selected else ())}
loaded = middleware.before_agent({}, None, None)["skills_metadata"]
assert {skill["name"] for skill in loaded} == expected, loaded
loaded = asyncio.run(middleware.abefore_agent({}, None, None))["skills_metadata"]
assert {skill["name"] for skill in loaded} == expected, loaded
# A catalog already in (checkpointed) state from an earlier selection is filtered too.
stale = SkillsMiddleware(backend=backend, sources=sources).before_agent({}, None, None)
assert {skill["name"] for skill in stale["skills_metadata"]} == {"operator-skill", *seeded}
update = middleware.before_agent(stale, None, None)
if selected:
    assert update is None
else:
    assert {skill["name"] for skill in update["skills_metadata"]} == {"operator-skill"}

requests = []

class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        requests.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

graph = create_deep_agent(
    model=Model(messages=iter([AIMessage(content="done")])), tools=[],
    system_prompt="S", backend=backend,
    middleware=[MimirFilesystemMiddleware(backend=backend), middleware],
)
graph.invoke({"messages": [HumanMessage(content="go")]})
prompt = "\n".join(
    str(message.content) for request in requests for message in request
    if isinstance(message, SystemMessage)
)
assert "operator-skill" in prompt
for skill in seeded:
    assert (f"**{skill}**" in prompt) is selected, (skill, prompt)

# --- the other agent-visible home-skill paths ---------------------------------
from mimir.chat_skills import ChatSkillRegistry
from mimir.skill_defs import home_builtin_skills_dir, home_skills_dir, installed_skill_names
from mimir.skill_resolver import find_skill_for_channel

config = SimpleNamespace(home=home, plugin_manager=manager, chat_skills_enabled=True,
                         chat_skill_allowlist=("operator-skill", *seeded))
chat = {skill.descriptor.name for skill in ChatSkillRegistry.from_config(config)._discoverable}
assert chat == expected, chat
hidden = hidden_home_skill_dirs(home, policy)
dirs = (home_skills_dir(home), home_builtin_skills_dir(home))
assert find_skill_for_channel("poller:operator-watch", dirs, hidden=hidden) is not None
assert (find_skill_for_channel("poller:worklink-tool-pins", dirs, hidden=hidden) is not None) is selected
assert set(visible_skill_names(home, installed_skill_names(home), policy)) == expected

if not selected:
    assert "mimir_cultivate" not in sys.modules
'''


def test_seeded_cultivate_jobs_and_skills_withheld_when_deselected(tmp_path: Path):
    if importlib.util.find_spec("mimir_cultivate") is None:
        pytest.skip("mimir-cultivate is not installed")
    home = tmp_path / "home"
    operator = home / "skills" / "operator-skill"
    operator.mkdir(parents=True)
    (operator / "SKILL.md").write_text(
        "---\nname: operator-skill\ndescription: Operator authored skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (operator / "pollers.json").write_text(json.dumps({"pollers": [{
        "name": "operator-watch", "command": "python3 poller.py", "cron": "*/5 * * * *",
    }]}), encoding="utf-8")
    # Cultivate ships no scheduler defaults today; a seeded ``cultivate-<job>``
    # entry is what seed_plugin_defaults would merge if it did.
    (home / "scheduler.yaml").write_text(yaml.safe_dump([
        {"name": "operator-daily", "prompt": "operator tick", "cron": "0 9 * * *"},
        {"name": "cultivate-nightly", "prompt": "seeded tick", "cron": "0 2 * * *"},
        {"name": "cultivate-callback", "callable": "operator-cb", "cron": "0 3 * * *"},
    ]), encoding="utf-8")
    env = {**os.environ, "MIMIR_HOME": str(home)}

    def run(mode: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT, mode, str(home), json.dumps(_SEEDED_SKILLS)],
            env={**env, "MIMIR_PLUGINS": "" if mode == "disabled" else "cultivate"},
            capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, f"{mode}: {result.stderr[-4000:]}"

    run("seed")
    run("disabled")
    run("selected")
