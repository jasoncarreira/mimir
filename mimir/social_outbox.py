"""The restricted social-cli outbox format, shared by submission and dispatch."""

from __future__ import annotations

import yaml


class OutboxLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys, including inside action payloads."""


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or non-string outbox key")
        result[key] = loader.construct_object(value_node)
    return result


OutboxLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_outbox(text: str) -> object:
    """Parse YAML with the same unambiguous mapping rules at both boundaries."""
    return yaml.load(text, Loader=OutboxLoader)


# social-cli OutboxAction fields (PR #2203's amended contract). Media is
# deliberately excluded: local file contents are not part of the reviewed PR.
_FIELDS = {
    "reply": ({"platform", "id", "text"}, {"notificationId", "idempotencyKey"}),
    "post": (set(), {"text", "platform", "platforms", "quoteId", "replyTo", "idempotencyKey"}),
    "thread": ({"platform", "posts"}, {"replyTo", "idempotencyKey"}),
    "like": ({"platform", "id"}, set()),
    "follow": ({"platform", "handle"}, set()),
    "bookmark": ({"platform", "id"}, {"text"}),
    "highlight": ({"platform", "id", "quote"}, {"text"}),
    "annotate": ({"platform", "id", "text"}, {"motivation", "quote"}),
    "ignore": ({"id"}, {"reason"}),
}
_PLATFORMS = {"bsky", "x"}


def _action_platforms(action: str, fields: dict) -> set[str]:
    """Collect declared, recognized platforms without inferring ignored targets."""
    if action == "ignore":
        return set()
    targets = fields.get("platforms", [fields.get("platform")]) if action == "post" else [fields.get("platform")]
    if not isinstance(targets, (list, dict)):
        return set()
    return {target for target in targets if isinstance(target, str) and target in _PLATFORMS}


def outbox_platform(doc: object) -> str | None:
    """Return a validated file's sole platform; ignore-only files have none."""
    errors = validate_outbox(doc)
    if errors:
        raise ValueError("; ".join(errors))
    platforms = set().union(*(_action_platforms(action, fields)
                             for item in doc["dispatch"] for action, fields in item.items()))
    return next(iter(platforms), None)


def validate_outbox(doc: object) -> list[str]:
    """Validate the reviewed action schema, explicitly refusing local media."""
    if not isinstance(doc, dict) or "dispatch" not in doc or set(doc) - {"dispatch", "processed"}:
        return ["outbox must contain dispatch and optionally processed only"]
    errors: list[str] = []
    if "processed" in doc and (
        not isinstance(doc["processed"], list)
        or any(not isinstance(value, str) for value in doc["processed"])
    ):
        errors.append("processed must be a list of strings")
    items = doc["dispatch"]
    if not isinstance(items, list) or not items:
        return errors + ["dispatch must be a non-empty list"]
    platforms: set[str] = set()
    for index, item in enumerate(items):
        label = f"item {index}"
        if not isinstance(item, dict) or len(item) != 1:
            errors.append(f"{label}: expected a single-key action mapping, e.g. - like: {{platform: bsky, id: 'at://...'}}; not action: type")
            continue
        action, fields = next(iter(item.items()))
        if action == "action":
            errors.append(f"{label}: old action: shape; use - like: {{platform: bsky, id: 'at://...'}}")
            continue
        if action not in _FIELDS:
            errors.append(f"{label}: unsupported action {action!r}; supported: {', '.join(_FIELDS)}")
            continue
        if not isinstance(fields, dict):
            errors.append(f"{label}: {action} fields must be a mapping")
            continue
        required, optional = _FIELDS[action]
        missing = required - fields.keys()
        unknown = fields.keys() - required - optional - {"media"}
        if "media" in fields:
            errors.append(f"{label}: {action} media is forbidden: local file contents are not part of the reviewed outbox PR")
        if missing:
            errors.append(f"{label}: {action} missing {', '.join(sorted(missing))}")
        if unknown:
            errors.append(f"{label}: {action} unknown field {', '.join(sorted(unknown))}")
        platforms.update(_action_platforms(action, fields))
        if action == "post":
            if "quoteId" in fields and "replyTo" in fields:
                errors.append(f"{label}: post cannot have both 'quoteId' and 'replyTo'")
            targets = {"platform", "platforms"} & fields.keys()
            if len(targets) != 1:
                errors.append(f"{label}: post needs exactly one of platform or platforms")
            if "platforms" in fields and isinstance(fields["platforms"], dict):
                if "text" in fields:
                    errors.append(f"{label}: post per-platform text mapping must omit text")
            elif "text" not in fields:
                errors.append(f"{label}: post missing text")
        for key, value in fields.items():
            if key == "posts" and action == "thread":
                if not isinstance(value, list) or not value or any(not isinstance(p, str) or not p.strip() for p in value):
                    errors.append(f"{label}: posts must be a non-empty list of strings")
            elif key == "platforms" and action == "post":
                if isinstance(value, list):
                    if not value or any(not isinstance(p, str) or p not in _PLATFORMS for p in value) or len(set(map(str, value))) != len(value):
                        errors.append(f"{label}: platforms must be a non-empty list of distinct platform names")
                elif isinstance(value, dict):
                    if not value or any(not isinstance(p, str) or p not in _PLATFORMS or not isinstance(t, str) or not t.strip() for p, t in value.items()):
                        errors.append(f"{label}: platforms must map platform names to non-empty strings")
                else:
                    errors.append(f"{label}: platforms must be a list or mapping")
            elif key in required | optional or (action == "post" and key == "text"):
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{label}: {key} must be a non-empty string")
                elif key == "platform" and value not in _PLATFORMS:
                    errors.append(f"{label}: unknown platform {value!r}")
    if len(platforms) > 1:
        errors.append("outbox actions span multiple platforms; split the file per platform")
    return errors
