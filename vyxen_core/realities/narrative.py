from dataclasses import dataclass
from typing import Optional

from ..actions import ActionIntent
from ..identity import IdentityCore
from ..memory import CausalMemory, extract_topics
from ..state import InternalState
from ..stimuli import Stimulus
from .base import RealityOutput


@dataclass
class NarrativeReality:
    name: str = "NarrativeReality"

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        recent = memory.fetch_recent(
            stimulus.context.get("server_id", "global"), limit=3
        )
        narrative_pressure = min(1.0, state.narrative_load + len(recent) * 0.05)
        confidence = 0.3 + identity.values["patience"] * 0.3
        risk = 0.2 + narrative_pressure * 0.2

        recommended: Optional[ActionIntent] = None
        if stimulus.type == "discord_message" and stimulus.routing != "directed":
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=confidence * 0.5,
                risk=risk,
                justification="Ambient chatter; no narrative push.",
            )

        if stimulus.type == "discord_message":
            # Narrative continuity should be subtle. Avoid emitting procedural
            # "thread maintenance" messages unless the user explicitly asks for a recap.
            content = (stimulus.context.get("content") or "").strip()
            lowered = content.lower()
            if self._wants_recap(lowered):
                recap = self._derive_recap(recent, stimulus, memory)
                if recap:
                    recommended = ActionIntent(
                        type="reply",
                        target_id=stimulus.context.get("channel_id"),
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": recap,
                        },
                        metadata={"recap": True},
                    )
                    confidence += 0.2
        elif stimulus.type == "silence":
            # Silence is fine; don't inject narrative unless asked.
            recommended = ActionIntent(
                type="observe",
                target_id=None,
                payload={},
                metadata={"reason": "silence_noop"},
            )

        return RealityOutput(
            reality=self.name,
            recommended_action=recommended,
            confidence=min(1.0, confidence),
            risk=min(1.0, risk),
            justification="Narrative continuity and thread maintenance.",
        )

    def _wants_recap(self, lowered: str) -> bool:
        if not lowered:
            return False
        return any(
            phrase in lowered
            for phrase in [
                "recap",
                "summary",
                "catch me up",
                "what did i miss",
                "what were we talking about",
                "remind me what we were talking about",
            ]
        )

    def _derive_recap(self, recent_entries, stimulus: Stimulus, memory: CausalMemory) -> str:
        server_id = stimulus.context.get("server_id", "global")
        recent_topics: list[str] = []
        for entry in recent_entries or []:
            try:
                for topic in entry.context.get("topics", []) or []:
                    if isinstance(topic, str) and topic:
                        recent_topics.append(topic)
            except Exception:
                continue

        content_topics = extract_topics(stimulus.context.get("content", "") or "")
        try:
            shared = memory.fetch_shared_context(server_id, content_topics)
            shared_topics = [topic for topic, _, _ in shared if isinstance(topic, str)]
        except Exception:
            shared_topics = []

        combined: list[str] = []
        for topic in [*shared_topics, *recent_topics]:
            if topic and topic not in combined:
                combined.append(topic)
            if len(combined) >= 6:
                break

        if not combined:
            return "Nothing major to catch up on—just normal chat."
        return "Quick recap: we’ve been talking about " + ", ".join(combined) + "."
