from dataclasses import dataclass
from typing import Optional

from ..actions import ActionIntent
from ..identity import IdentityCore
from ..memory import CausalMemory
from ..state import InternalState
from ..stimuli import Stimulus
from .base import RealityOutput


@dataclass
class StrategicReality:
    name: str = "StrategicReality"

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        curiosity = identity.values["curiosity"]
        caution = identity.values["caution"]
        confidence = 0.4 + curiosity * 0.3
        risk = 0.3 + caution * 0.2
        recommended: Optional[ActionIntent] = None

        # StrategicReality is deliberately non-chatty: social conversation should be
        # handled by SocialReality, and admin actions by ToolsReality. Avoid
        # scheduling generic "still processing" follow-ups that feel spammy.
        if stimulus.type == "discord_message":
            recommended = None
        elif stimulus.type == "silence":
            if state.risk_pressure > 0.6:
                recommended = ActionIntent(
                    type="observe",
                    target_id=None,
                    payload={},
                    metadata={"reason": "elevated risk; hold position"},
                )
                confidence += 0.1
                risk -= 0.1

        return RealityOutput(
            reality=self.name,
            recommended_action=recommended,
            confidence=min(1.0, confidence),
            risk=max(0.0, min(1.0, risk)),
            justification="Strategic horizon scanning and risk posture.",
        )
