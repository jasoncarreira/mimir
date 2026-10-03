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


_FIELDS = {
    "reply": ({"platform", "id", "text"}, set()),
    "post": (set(), {"text", "platform", "platforms"}),
    "thread": ({"platform", "posts"}, set()),
    "like": ({"platform", "id"}, set()),
    "annotate": ({"platform", "id", "text"}, {"motivation", "quote"}),
    "ignore": ({"id"}, {"reason"}),
}
_PLATFORMS = {"bsky", "x"}


def validate_outbox(doc: object) -> list[str]:
    """Return indexed schema errors for social-cli's documented dispatch actions."""
    if not isinstance(doc, dict) or set(doc) != {"dispatch"}:
        return ["outbox must contain only dispatch"]
    items = doc["dispatch"]
    if not isinstance(items, list) or not items:
        return ["dispatch must be a non-empty list"]
    errors: list[str] = []
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
        unknown = fields.keys() - required - optional
        if missing:
            errors.append(f"{label}: {action} missing {', '.join(sorted(missing))}")
        if unknown:
            errors.append(f"{label}: {action} unknown field {', '.join(sorted(unknown))}")
        if action == "post":
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
    return errors
