import time

from vyxen_core.conversation import SessionStore
from vyxen_core.stimuli import Stimulus


def test_session_store_routes_and_expires():
    store = SessionStore(ttl_seconds=1.0)

    mention_stim = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 1,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": True,
            "message_id": 123,
        },
        salience=0.6,
    )
    routing, session, expired = store.route_stimulus(mention_stim)
    assert routing == "directed"
    assert session is not None
    assert not expired

    # Ambient message without mention should not create a session
    ambient_stim = Stimulus(
        type="discord_message",
        source="discord",
        context={"author_id": 2, "server_id": "guild", "channel_id": 99, "mentions_bot": False},
        salience=0.3,
    )
    routing2, session2, _ = store.route_stimulus(ambient_stim)
    assert routing2 == "ambient"
    assert session2 is None

    # Force expiry
    session.expires_at = time.time() - 1
    ended = store.expire_stale()
    assert ended and ended[0][0].user_id == 1


def test_whitelisted_admin_command_is_directed_without_mention():
    store = SessionStore(ttl_seconds=30.0)
    stim = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 42,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": False,
            "author_whitelisted": True,
            "message_id": 1,
            "content": 'create category "test" and channel "test" then create role "test"',
        },
        salience=0.6,
    )
    routing, session, _ = store.route_stimulus(stim)
    assert routing == "ambient"
    assert session is None


def test_greeting_is_directed_without_mention():
    store = SessionStore(ttl_seconds=30.0)
    stim = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 7,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": False,
            "message_id": 2,
            "content": "hello",
        },
        salience=0.3,
    )
    routing, session, _ = store.route_stimulus(stim)
    assert routing == "ambient"
    assert session is None


def test_non_whitelisted_admin_command_stays_ambient_without_mention():
    store = SessionStore(ttl_seconds=30.0)
    stim = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 8,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": False,
            "author_whitelisted": False,
            "message_id": 3,
            "content": 'create role "test"',
        },
        salience=0.5,
    )
    routing, session, _ = store.route_stimulus(stim)
    assert routing == "ambient"
    assert session is None


def test_session_switches_on_new_mention():
    store = SessionStore(ttl_seconds=30.0)
    first = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 1,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": True,
            "message_id": 10,
            "content": "hi @Vyxen",
        },
        salience=0.6,
    )
    routing, session, ended = store.route_stimulus(first)
    assert routing == "directed"
    assert session is not None
    assert not ended

    second = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 2,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": True,
            "message_id": 11,
            "content": "my turn @Vyxen",
        },
        salience=0.6,
    )
    routing2, session2, ended2 = store.route_stimulus(second)
    assert routing2 == "directed"
    assert session2 is not None and session2.user_id == 2
    assert ended2 and ended2[0][0].user_id == 1

    third = Stimulus(
        type="discord_message",
        source="discord",
        context={
            "author_id": 1,
            "server_id": "guild",
            "channel_id": 99,
            "mentions_bot": False,
            "message_id": 12,
            "content": "i shouldn't get a reply now",
        },
        salience=0.4,
    )
    routing3, session3, _ = store.route_stimulus(third)
    assert routing3 == "ambient"
    assert session3 is None
