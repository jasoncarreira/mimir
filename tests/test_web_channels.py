from mimir.web_channels import DEFAULT_WEB_CHANNEL, web_channel_for_identity


def test_web_channel_preserves_normal_canonicals_verbatim():
    assert web_channel_for_identity("alice") == "web-alice"
    assert web_channel_for_identity("a.b") != web_channel_for_identity("a_b")
    assert web_channel_for_identity("Alice") != web_channel_for_identity("alice")


def test_web_channel_escapes_reserved_default_collision():
    assert web_channel_for_identity("") == DEFAULT_WEB_CHANNEL
    assert web_channel_for_identity("default") != DEFAULT_WEB_CHANNEL
    assert web_channel_for_identity("default").startswith("web-user:")


def test_web_channel_escapes_escape_namespace_collision():
    canonical = "user:abc"
    assert web_channel_for_identity(canonical) != f"web-{canonical}"
    assert web_channel_for_identity(canonical).startswith("web-user:")
