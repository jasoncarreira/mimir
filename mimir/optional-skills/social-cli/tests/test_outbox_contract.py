"""The dispatch schema accepts archived and documented social-cli files."""

from pathlib import Path

import pytest

from mimir.social_outbox import load_outbox, validate_outbox


@pytest.mark.parametrize("name", ["archived_outbox.yaml", "readme_outbox.yaml"])
def test_successful_social_cli_documents(name):
    text = (Path(__file__).parent / "fixtures" / name).read_text()
    assert validate_outbox(load_outbox(text)) == []


@pytest.mark.parametrize("item,problem", [
    ("action: like, uri: 'at://post', cid: abc", "not action: type"),
    ("reply: {platform: bsky, text: hi}", "missing id"),
    ("post: {platform: bsky, platforms: [x], text: hi}", "exactly one"),
    ("thread: {platform: bsky, posts: []}", "posts must"),
    ("dance: {id: abc}", "unsupported action"),
    ("follow: {platform: bsky, id: abc}", "unsupported action"),
    ("like: {platform: bsky, id: abc, secret: no}", "unknown field"),
])
def test_schema_refusals_name_item_and_problem(item, problem):
    assert problem in " ".join(validate_outbox(load_outbox(f"dispatch:\n  - {{{item}}}\n")))
    assert "item 0" in " ".join(validate_outbox(load_outbox(f"dispatch:\n  - {{{item}}}\n")))
