from vyxen_core.realities.social import SocialReality
from vyxen_core.state import InternalState
from vyxen_core.stimuli import Stimulus
from vyxen_core.identity import IdentityCore
from vyxen_core.memory import CausalMemory
from vyxen_core.config import RuntimeConfig


def _stub_identity(config: RuntimeConfig) -> IdentityCore:
    return IdentityCore(
        config=config,
        values={t: 0.5 for t in ["assertiveness", "playfulness", "caution", "curiosity", "patience"]},
        allow_persistence=False,
    )


def test_social_reality_answers_name_from_memory(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=True)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = SocialReality(config=config)

    memory.save_important("guild", "123", "preferred_name", "DVS", weight=0.9)

    stim = Stimulus(
        type="discord_message",
        source="test",
        routing="directed",
        context={
            "content": "what is my name?",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "reply"
    assert "DVS" in out.recommended_action.payload["content"]


def test_social_reality_explains_no_raw_logs(tmp_path):
    config = RuntimeConfig(memory_path=tmp_path / "mem.db", audit_log_path=tmp_path / "audit.log")
    memory = CausalMemory(config, allow_writes=True)
    identity = _stub_identity(config)
    state = InternalState(safe_mode=False)
    reality = SocialReality(config=config)

    stim = Stimulus(
        type="discord_message",
        source="test",
        routing="directed",
        context={
            "content": "what was my previous message?",
            "channel_id": 1,
            "server_id": "guild",
            "author_id": 123,
            "message_id": 5,
        },
    )
    out = reality.interpret(stim, state, memory, identity)
    assert out.recommended_action is not None
    assert out.recommended_action.type == "reply"
    assert "raw chat logs" in out.recommended_action.payload["content"].lower()

