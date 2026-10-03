"""The dispatch schema accepts archived and documented social-cli files."""

from pathlib import Path

import pytest

from mimir.social_outbox import load_outbox, validate_outbox


@pytest.mark.parametrize("name", ["archived_outbox.yaml", "readme_outbox.yaml"])
def test_successful_social_cli_documents(name):
    text = (Path(__file__).parent / "fixtures" / name).read_text()
    assert validate_outbox(load_outbox(text)) == []


@pytest.mark.parametrize("item,problem", [
    ("action: like", "old action: shape"),
    ("action: like, uri: 'at://post', cid: abc", "not action: type"),
    ("reply: {platform: bsky, text: hi}", "missing id"),
    ("post: {platform: bsky, platforms: [x], text: hi}", "exactly one"),
    ("thread: {platform: bsky, posts: []}", "posts must"),
    ("dance: {id: abc}", "unsupported action"),
    ("follow: {platform: bsky, id: abc}", "missing handle"),
    ("like: {platform: bsky, id: abc, secret: no}", "unknown field"),
])
def test_schema_refusals_name_item_and_problem(item, problem):
    assert problem in " ".join(validate_outbox(load_outbox(f"dispatch:\n  - {{{item}}}\n")))
    assert "item 0" in " ".join(validate_outbox(load_outbox(f"dispatch:\n  - {{{item}}}\n")))


# Every accepted action and declared optional field in the amended validator
# contract. Pair acceptance with indexed unknown-field and media refusals.
ACTIONS = [
    ("reply", {"platform": "bsky", "id": "at://post", "text": "Hi",
               "notificationId": "notif_1", "idempotencyKey": "reply-1"}),
    ("post", {"platform": "bsky", "text": "Hi", "quoteId": "at://quote",
              "idempotencyKey": "post-1"}),
    ("thread", {"platform": "bsky", "posts": ["One", "Two"],
                "replyTo": "at://parent", "idempotencyKey": "thread-1"}),
    ("like", {"platform": "bsky", "id": "at://post"}),
    ("follow", {"platform": "bsky", "handle": "example.bsky.social"}),
    ("bookmark", {"platform": "bsky", "id": "at://post", "text": "Save"}),
    ("highlight", {"platform": "bsky", "id": "at://post", "quote": "Excerpt", "text": "Note"}),
    ("annotate", {"platform": "bsky", "id": "at://post", "text": "Note",
                  "motivation": "commenting", "quote": "Excerpt"}),
    ("ignore", {"id": "notif_1", "reason": "spam"}),
]


@pytest.mark.parametrize("action,payload", ACTIONS)
def test_amended_action_contract(action, payload):
    assert validate_outbox({"dispatch": [{action: payload}], "processed": ["notif_1"]}) == []
    errors = validate_outbox({"dispatch": [{action: {**payload, "unknown": "value"}}]})
    assert any("item 0" in error and "unknown field unknown" in error for error in errors)


@pytest.mark.parametrize("action,payload", ACTIONS)
@pytest.mark.parametrize("media", [["local-image.png"], [], "local-image.png", None])
def test_media_is_forbidden_on_every_action(action, payload, media):
    errors = validate_outbox({"dispatch": [{action: {**payload, "media": media}}]})
    assert any("item 0" in error and "media is forbidden" in error for error in errors)
    assert any("reviewed outbox PR" in error for error in errors)


@pytest.mark.parametrize("action", ["bookmark", "highlight"])
def test_optional_annotation_text_can_be_omitted(action):
    payload = {"platform": "bsky", "id": "at://post"}
    if action == "highlight":
        payload["quote"] = "Excerpt"
    assert validate_outbox({"dispatch": [{action: payload}]}) == []


@pytest.mark.parametrize("processed", [None, "notif_1", {}, [1], [False]])
def test_processed_requires_a_string_list(processed):
    errors = validate_outbox({"dispatch": [{"ignore": {"id": "notif_1"}}], "processed": processed})
    assert "processed must be a list of strings" in errors


@pytest.mark.parametrize("entries", [
    [{"post": {"text": "Hi", "platforms": ["bsky", "x"]}}],
    [{"post": {"platforms": {"bsky": "Hi", "x": "Hello"}}}],
    [{"reply": {"platform": "bsky", "id": "post", "text": "Hi"}},
     {"like": {"platform": "x", "id": "123"}}],
    [{"follow": {"platform": "bsky", "handle": "example.bsky.social"}},
     {"bookmark": {"platform": "x", "id": "123"}}],
])
def test_mixed_platform_outboxes_require_split(entries):
    errors = validate_outbox({"dispatch": entries})
    assert "outbox actions span multiple platforms; split the file per platform" in errors


@pytest.mark.parametrize("platform", ["bsky", "x"])
@pytest.mark.parametrize("form", ["platform", "list", "mapping", "follow"])
def test_single_platform_selection_ignores_platformless_actions(platform, form):
    from mimir.social_outbox import outbox_platform

    payload = {"platform": platform, "text": "Hi"}
    if form == "list":
        payload = {"platforms": [platform], "text": "Hi"}
    elif form == "mapping":
        payload = {"platforms": {platform: "Hi"}}
    entry = {"post": payload} if form != "follow" else {"follow": {"platform": platform, "handle": "example"}}
    doc = {"dispatch": [entry, {"ignore": {"id": "notif"}}]}
    assert validate_outbox(doc) == []
    assert outbox_platform(doc) == platform
    assert outbox_platform({"dispatch": [{"ignore": {"id": "notif"}}]}) is None


@pytest.mark.parametrize("field", ["quoteId", "replyTo"])
def test_post_accepts_either_target_but_not_both(field):
    payload = {"platform": "bsky", "text": "Hi", field: "at://target"}
    assert validate_outbox({"dispatch": [{"post": payload}]}) == []
    errors = validate_outbox({"dispatch": [{"post": {**payload, "quoteId": "q", "replyTo": "r"}}]})
    assert "item 0: post cannot have both 'quoteId' and 'replyTo'" in errors


def test_processed_empty_list_and_unknown_top_level_field():
    doc = {"dispatch": [{"ignore": {"id": "notif_1"}}], "processed": []}
    assert validate_outbox(doc) == []
    assert validate_outbox({**doc, "notifications": []})


@pytest.mark.parametrize("action,field", [
    ("reply", "notificationId"), ("reply", "idempotencyKey"),
    ("post", "quoteId"), ("post", "replyTo"), ("post", "idempotencyKey"),
    ("thread", "replyTo"), ("thread", "idempotencyKey"),
    ("annotate", "motivation"), ("ignore", "reason"),
])
def test_optional_fields_reject_wrong_types(action, field):
    payload = next(payload for name, payload in ACTIONS if name == action)
    errors = validate_outbox({"dispatch": [{action: {**payload, field: []}}]})
    assert any(f"{field} must be a non-empty string" in error for error in errors)
