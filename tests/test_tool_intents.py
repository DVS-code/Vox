from vyxen_core.stimuli import Stimulus
from vyxen_core.tool_intents import parse_natural_language_intent


def test_parse_bulk_setup_intent_with_quotes():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": 'create a new text channel and category and name them both "test" then create a new role called test and set permissions for the role to see the channel',
            "channel_id": 123,
            "server_id": "guild",
            "channel_mentions": [],
            "role_mentions": [],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "bulk_setup"
    assert parsed.requested_changes["category_name"] == "test"
    assert parsed.requested_changes["channel_name"] == "test"
    assert parsed.requested_changes["role_name"] == "test"
    assert parsed.requested_changes["permissions"]["view_channel"] is True


def test_parse_create_role_intent():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "create role 'Mods'", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_role"
    assert parsed.requested_changes["role_name"].lower() == "mods"


def test_parse_create_role_intent_without_quotes():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "create role Mods", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_role"
    assert parsed.requested_changes["role_name"].lower() == "mods"


def test_parse_create_role_strips_politeness_suffix():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "create role DVS please", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_role"
    assert parsed.requested_changes["role_name"] == "DVS"


def test_parse_bulk_setup_intent_with_distinct_quoted_names():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": 'create category "TestCat" and channel "test-room" then create role "TestRole" and give the role access to the channel',
            "channel_id": 123,
            "server_id": "guild",
            "channel_mentions": [],
            "role_mentions": [],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type in {"bulk_setup", "server_setup"}
    assert parsed.requested_changes["category_name"] == "TestCat"
    assert parsed.requested_changes["channel_name"] == "test-room"
    assert parsed.requested_changes["role_name"] == "TestRole"
    assert parsed.requested_changes["permissions"]["view_channel"] is True


def test_parse_bulk_setup_with_multiple_permissions():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": (
                'create category "TestCat" and channel "test-room" then create role "TestRole" '
                'and give the role access to the channel and allow send messages and attach files '
                "but deny mention everyone"
            ),
            "channel_id": 123,
            "server_id": "guild",
            "channel_mentions": [],
            "role_mentions": [],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type in {"bulk_setup", "server_setup"}
    perms = parsed.requested_changes["permissions"]
    assert perms["view_channel"] is True
    assert perms["send_messages"] is True
    assert perms["attach_files"] is True
    assert perms["mention_everyone"] is False


def test_parse_permission_overwrite_update_from_mentions():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "set permissions for @TestRole in #test: allow send messages, deny embed links",
            "channel_id": 1,
            "server_id": "guild",
            "channel_mentions": [555],
            "role_mentions": [777],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "permission_check_and_fix"
    assert parsed.target_channel == 555
    assert parsed.target_role == 777
    assert parsed.requested_changes["permissions"]["send_messages"] is True
    assert parsed.requested_changes["permissions"]["embed_links"] is False


def test_parse_create_voice_channel_intent():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": 'create new voice channel called "chill zone"', "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_voice_channel"
    assert parsed.requested_changes["channel_name"] == "chill zone"
    assert parsed.requested_changes["channel_type"] == "voice"


def test_parse_create_text_channel_under_category_with_called_and_quotes():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": 'vyxen can you create a new text channel under "test" category called "test 3"',
            "channel_id": 1,
            "server_id": "guild",
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_text_channel"
    assert parsed.requested_changes["category_name"] == "test"
    assert parsed.requested_changes["channel_name"] == "test 3"


def test_parse_implicit_text_channel_under_category():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": 'text channel "test 3" under test category',
            "channel_id": 1,
            "server_id": "guild",
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "create_text_channel"
    assert parsed.requested_changes["category_name"] == "test"
    assert parsed.requested_changes["channel_name"] == "test 3"


def test_parse_move_channel_to_category_with_quotes():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": 'move the "chill-zone" text channel to under the "test" category',
            "channel_id": 1,
            "server_id": "guild",
            "channel_mentions": [],
            "role_mentions": [],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "move_channel_to_category"
    assert parsed.requested_changes["channel_name"] == "chill-zone"
    assert parsed.requested_changes["category_name"] == "test"


def test_parse_lock_category_admin_only():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "can you lock the admin category so only admin role can see it?",
            "channel_id": 1,
            "server_id": "guild",
            "channel_mentions": [],
            "role_mentions": [],
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "lock_category"
    assert parsed.requested_changes["category_name"] == "admin"
    assert parsed.requested_changes["role_name"] == "admin"
    assert parsed.requested_changes["strict"] is True


def test_parse_delete_role_intent_with_confirm_flag():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": 'delete role "OldRole"', "channel_id": 1, "server_id": "guild", "role_mentions": []},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "delete_role"
    assert parsed.requested_changes["role_name"] == "OldRole"
    assert parsed.requested_changes["confirmed"] is False

    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": 'confirm delete role "OldRole"', "channel_id": 1, "server_id": "guild", "role_mentions": []},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "delete_role"
    assert parsed2.requested_changes["confirmed"] is True


def test_parse_ban_and_timeout_member_intents():
    member_id = "123456789012345678"
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": f"ban member {member_id}", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "ban_member"
    assert parsed.requested_changes["member_id"] == member_id
    assert parsed.requested_changes["confirmed"] is False

    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": f"mute member {member_id} for 15m", "channel_id": 1, "server_id": "guild"},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "timeout_member"
    assert parsed2.requested_changes["member_id"] == member_id
    assert parsed2.requested_changes["duration_seconds"] == 15 * 60


def test_parse_list_roles_query():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "what roles are in this server?", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "list_roles"

def test_parse_list_channels_query():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "what channels are in this server?", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "list_channels"


def test_parse_server_stats_report():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "can you pull up some server stats like member count and channel count", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "server_stats_report"


def test_parse_assign_role_to_me():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "assign the DVS role to me please", "channel_id": 1, "server_id": "guild", "author_id": 123},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "assign_role"
    assert parsed.requested_changes["role_name"] == "DVS"
    assert parsed.requested_changes["member_id"] == "123"


def test_parse_role_permissions_update_admin_permission():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "assign administrator permissions to the admin role", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "role_permissions_update"
    assert parsed.requested_changes["role_name"] == "admin"
    assert parsed.requested_changes["permissions"]["administrator"] is True


def test_parse_role_permissions_and_user_profile_reports():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "audit permissions for the admin role", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "role_permissions_report"
    assert parsed.requested_changes["role_name"] == "admin"

    user_id = "123456789012345678"
    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": f"tell me about user {user_id}", "channel_id": 1, "server_id": "guild"},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "user_profile_report"
    assert parsed2.requested_changes["user_id"] == user_id


def test_parse_last_action_queries_and_dry_run():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "what did you just change?", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "last_action_explain"
    assert parsed.dry_run is False

    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "dry run: create role Test", "channel_id": 1, "server_id": "guild"},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "create_role"
    assert parsed2.dry_run is True


def test_parse_server_activity_report():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "what changed recently?", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "server_activity_report"


def test_parse_save_and_run_macro_and_schedule():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": 'save macro "lockdown" = lock all channels', "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "save_macro"
    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "run macro lockdown", "channel_id": 1, "server_id": "guild"},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "run_macro"

    stim3 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "schedule delete channel #general in 2h", "channel_id": 1, "server_id": "guild"},
    )
    parsed3 = parse_natural_language_intent(stim3)
    assert parsed3 is not None
    assert parsed3.intent_type == "schedule_action"


def test_parse_permission_explain_and_role_preview():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "why can't @user talk in #general", "channel_id": 1, "server_id": "guild", "mentioned_user_ids": [123], "channel_mentions": [9]},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "permission_explain"

    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "what would happen if I give @user admin", "channel_id": 1, "server_id": "guild", "mentioned_user_ids": [123]},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "role_impact_preview"


def test_parse_quarantine_member_intent():
    user_id = "123456789012345678"
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": f"setup a quarantine category and channel quarantine with new role quarantine and assign it to member {user_id}",
            "channel_id": 1,
            "server_id": "guild",
        },
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "quarantine_member"
    assert parsed.requested_changes["member_id"] == user_id


def test_parse_add_faq():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": 'add faq "How to verify?" = Click the button', "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "add_faq"
    assert parsed.requested_changes["question"] == "How to verify?"
    assert parsed.requested_changes["answer"] == "Click the button"


def test_parse_answer_faq():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "faq verification steps", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "answer_faq"
    assert parsed.requested_changes["question"] == "verification steps"
    assert parsed.requires_admin is False


def test_parse_welcome_message():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "draft welcome message for the car club", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "draft_welcome_message"
    assert "car club" in parsed.requested_changes["focus"]


def test_parse_setup_wizard_start_and_progress():
    stim = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "start setup wizard", "channel_id": 1, "server_id": "guild"},
    )
    parsed = parse_natural_language_intent(stim)
    assert parsed is not None
    assert parsed.intent_type == "setup_wizard_start"

    stim2 = Stimulus(
        type="discord_message",
        source="test",
        context={"content": "car community", "channel_id": 1, "server_id": "guild", "setup_wizard_active": True},
    )
    parsed2 = parse_natural_language_intent(stim2)
    assert parsed2 is not None
    assert parsed2.intent_type == "setup_wizard_progress"
    assert parsed2.requested_changes["answer"] == "car community"
