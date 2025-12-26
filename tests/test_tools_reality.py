from vyxen_core.realities.tools import ToolsReality
from vyxen_core.state import InternalState
from vyxen_core.stimuli import Stimulus
from vyxen_core.identity import IdentityCore
from vyxen_core.memory import CausalMemory
from vyxen_core.config import RuntimeConfig


def _stub_identity(config: RuntimeConfig) -> IdentityCore:
    return IdentityCore(config=config, values={t: 0.5 for t in ["assertiveness", "playfulness", "caution", "curiosity", "patience"]}, allow_persistence=False)


def test_tools_reality_requires_admin_for_execution(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=False)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = ToolsReality(enabled=True, dry_run=True)

    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "create role 'Test'",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
            "author_permissions": {"administrator": False, "manage_permissions": False},
            "author_whitelisted": False,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "reply"


def test_tools_reality_emits_tool_call_for_admin(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=False)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = ToolsReality(enabled=True, dry_run=False)

    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "create role 'Test'",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
            "author_permissions": {"administrator": True, "manage_permissions": True},
            "author_whitelisted": False,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "tool_call"
    assert out.recommended_action.payload["intent_type"] == "create_role"


def test_tools_reality_list_roles_is_not_guidance(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=False)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = ToolsReality(enabled=True, dry_run=False)

    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "what roles are in this server?",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
            "author_permissions": {"administrator": True, "manage_permissions": True},
            "author_whitelisted": False,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "tool_call"
    assert out.recommended_action.payload["intent_type"] == "list_roles"


def test_tools_reality_dry_run_override(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=False)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = ToolsReality(enabled=True, dry_run=False)

    stim = Stimulus(
        type="discord_message",
        source="test",
        context={
            "content": "dry run: create role 'Test'",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
            "author_permissions": {"administrator": True, "manage_permissions": True},
            "author_whitelisted": False,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "tool_call"
    assert out.recommended_action.metadata["dry_run"] is True
