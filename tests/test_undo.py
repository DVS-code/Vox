import asyncio
import types

from discord_adapter import DiscordAdapter
from vyxen_core.action_journal import ActionEntry


class DummyRole:
    def __init__(self, role_id: int, name: str = "r", perms: int = 0):
        self.id = role_id
        self.name = name
        self.permissions = types.SimpleNamespace(value=perms)
        self.deleted = False

    async def delete(self, reason: str = ""):
        self.deleted = True

    async def edit(self, permissions=None, reason: str = ""):
        self.permissions = permissions


class DummyChannel:
    def __init__(self, cid: int, category_id=None):
        self.id = cid
        self.category_id = category_id
        self.deleted = False
        self.moved_to = None
        self._overwrites = {}

    async def delete(self, reason: str = ""):
        self.deleted = True

    async def edit(self, category=None, reason: str = ""):
        self.moved_to = category.id if category else None

    def overwrites_for(self, role):
        return DummyOverwrite(self._overwrites.get(role.id, {}))

    async def set_permissions(self, role, overwrite=None, reason: str = ""):
        self._overwrites[role.id] = dict(overwrite.data)


class DummyOverwrite:
    def __init__(self, data):
        self.data = data.copy()

    def __iter__(self):
        for k, v in self.data.items():
            yield k, v

    def __getattr__(self, item):
        return self.data.get(item)

    def __setattr__(self, key, value):
        if key == "data":
            super().__setattr__(key, value)
        else:
            self.data[key] = value


class DummyGuild:
    def __init__(self):
        self.roles = {}
        self.channels = {}

    def get_role(self, rid):
        return self.roles.get(int(rid))

    def get_channel(self, cid):
        return self.channels.get(int(cid))


def test_undo_create_role_deletes_role():
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter._serialize_overwrites = lambda x: {}
    guild = DummyGuild()
    role = DummyRole(1, "Test")
    guild.roles[1] = role
    entry = ActionEntry(
        user_id="u",
        action_type="create_role",
        targets={"role_id": 1},
        before_state=None,
        after_state={"role_id": 1},
        reversible=True,
    )
    res = asyncio.run(DiscordAdapter._undo_action(adapter, entry, guild, None, "u"))
    assert res.success is True
    assert role.deleted is True


def test_undo_move_channel_restores_category():
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter._serialize_overwrites = lambda x: {}
    guild = DummyGuild()
    ch = DummyChannel(10, category_id=5)
    cat_prev = DummyChannel(5)
    cat_new = DummyChannel(6)
    guild.channels[10] = ch
    guild.channels[5] = cat_prev
    guild.channels[6] = cat_new
    entry = ActionEntry(
        user_id="u",
        action_type="move_channel_to_category",
        targets={"channel_id": 10},
        before_state={"from_category_id": 5},
        after_state={"to_category_id": 6},
        reversible=True,
    )
    res = asyncio.run(DiscordAdapter._undo_action(adapter, entry, guild, None, "u"))
    assert res.success is True
    assert ch.moved_to == 5
