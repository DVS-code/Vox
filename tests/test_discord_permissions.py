from vyxen_core.discord_permissions import parse_permission_overwrites, resolve_permission_flag


def test_resolve_permission_flag_from_human_phrase():
    assert resolve_permission_flag("send messages") == "send_messages"
    assert resolve_permission_flag("embed links") == "embed_links"
    assert resolve_permission_flag("VIEW_CHANNEL") == "view_channel"


def test_parse_permission_overwrites_allow_deny_lists():
    result = parse_permission_overwrites("allow send messages and attach files; deny mention everyone")
    assert result.overwrites["send_messages"] is True
    assert result.overwrites["attach_files"] is True
    assert result.overwrites["mention_everyone"] is False


def test_parse_permission_overwrites_assignment_style():
    result = parse_permission_overwrites("send_messages=false, manage roles: true, embed links: unset")
    assert result.overwrites["send_messages"] is False
    assert result.overwrites["manage_roles"] is True
    assert result.overwrites["embed_links"] is None

