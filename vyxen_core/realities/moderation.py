from dataclasses import dataclass
from typing import Optional

from ..actions import ActionIntent
from ..identity import IdentityCore
from ..memory import CausalMemory
from ..state import InternalState
from ..stimuli import Stimulus
from .base import RealityOutput


@dataclass
class ModerationReality:
    name: str = "ModerationReality"
    enabled: bool = True
    dry_run: bool = True

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        if state.safe_mode or not self.enabled:
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=0.1,
                risk=0.05,
                justification="Moderation muted in safe mode.",
            )
        risk = min(1.0, 0.3 + stimulus.salience * 0.4)
        confidence = 0.4 + identity.values["caution"] * 0.4
        recommended: Optional[ActionIntent] = None

        if stimulus.type == "discord_message":
            content = stimulus.context.get("content", "")
            toxicity = self._estimate_toxicity(content)
            risk = min(1.0, risk + toxicity * 0.5)
            if toxicity > 0.6:
                recommended = ActionIntent(
                    type="defer",
                    target_id=stimulus.context.get("channel_id"),
                    payload={
                        "reason": "high-risk content",
                        "message_id": stimulus.context.get("message_id"),
                    },
                    metadata={"toxicity": toxicity},
                )
                confidence += 0.2
        elif stimulus.type.startswith("discord_member_"):
            recommended = ActionIntent(
                type="observe",
                target_id=None,
                payload={},
                metadata={"reason": "membership change monitoring"},
            )
            confidence = 0.3
            risk *= 0.8
        elif stimulus.type == "attachment":
            recommended = ActionIntent(
                type="defer",
                target_id=stimulus.context.get("channel_id"),
                payload={"reason": "attachment review"},
                metadata={"attachment": stimulus.context.get("attachment_id")},
            )
            risk += 0.2
            confidence += 0.1

        if self.dry_run and recommended is not None:
            recommended = ActionIntent(
                type="observe",
                target_id=recommended.target_id,
                payload={"reason": "dry_run", "original": recommended.to_dict()},
                metadata={"from": "moderation_dry_run"},
            )
            risk *= 0.5
            confidence *= 0.5

        return RealityOutput(
            reality=self.name,
            recommended_action=recommended,
            confidence=min(1.0, confidence),
            risk=min(1.0, risk),
            justification="Moderation check for risky or policy-violating content.",
        )

    def _estimate_toxicity(self, content: str) -> float:
        lowered = content.lower()
        danger_keywords = ["hate", "kill", "attack", "bomb", "racist"]
        hits = sum(1 for word in danger_keywords if word in lowered)
        return min(1.0, hits * 0.2 + (0.1 if len(content) > 280 else 0.0))
