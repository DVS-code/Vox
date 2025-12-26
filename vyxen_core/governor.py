from dataclasses import dataclass
from typing import List, Optional

from .actions import ActionIntent
from .identity import IdentityCore
from .memory import CausalMemory
from .realities.base import RealityOutput


@dataclass
class GovernorDecision:
    action: ActionIntent
    confidence: float
    risk: float
    rationale: str

    def to_dict(self):
        return {
            "action": self.action.to_dict(),
            "confidence": self.confidence,
            "risk": self.risk,
            "rationale": self.rationale,
        }


class Governor:
    def __init__(self, identity: IdentityCore, memory: CausalMemory):
        self.identity = identity
        self.memory = memory

    def deliberate(
        self, server_id: str, realities: List[RealityOutput], directed: bool
    ) -> Optional[GovernorDecision]:
        best: Optional[GovernorDecision] = None
        fallback = ActionIntent(type="observe", target_id=None, payload={}, metadata={})
        best_score = -1.0

        if not directed:
            return GovernorDecision(
                action=fallback,
                confidence=0.35,
                risk=0.05,
                rationale="Ambient stimulus without active session or mention.",
            )

        recent = self.memory.fetch_recent(server_id, limit=6)
        memory_bias = 0.0
        if recent:
            memory_bias = sum(entry.confidence_delta for entry in recent) / len(recent)

        for output in realities:
            if output.recommended_action is None:
                continue
            score = self._score(output, memory_bias)
            if score > best_score:
                best_score = score
                best = GovernorDecision(
                    action=output.recommended_action,
                    confidence=output.confidence,
                    risk=output.risk,
                    rationale=output.justification,
                )

        if best is None:
            return GovernorDecision(
                action=fallback,
                confidence=0.3,
                risk=0.1,
                rationale="No strong recommendation; choosing deliberate observation.",
            )

        risk_threshold = 0.7 - memory_bias * 0.2
        if best.risk > risk_threshold and best.confidence < 0.7:
            # Override to silence when risk is too high relative to confidence
            return GovernorDecision(
                action=fallback,
                confidence=0.4,
                risk=0.05,
                rationale="Risk exceeded confidence; opting to hold position.",
            )

        return best

    def _score(self, output: RealityOutput, memory_bias: float) -> float:
        assertiveness = self.identity.values["assertiveness"]
        caution = self.identity.values["caution"]
        curiosity = self.identity.values["curiosity"]

        base = output.confidence * (1 - output.risk)
        modulation = 1 + 0.2 * (assertiveness - caution) + 0.1 * curiosity + memory_bias
        return base * modulation
