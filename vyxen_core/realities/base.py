from dataclasses import dataclass
from typing import Any, Dict, Protocol

from ..actions import ActionIntent
from ..stimuli import Stimulus
from ..identity import IdentityCore
from ..memory import CausalMemory
from ..state import InternalState


@dataclass
class RealityOutput:
    reality: str
    recommended_action: ActionIntent | None
    confidence: float
    risk: float
    justification: str

    def to_dict(self, include_metadata: bool = True) -> Dict[str, Any]:
        return {
            "reality": self.reality,
            "recommended_action": self.recommended_action.to_dict(include_metadata=include_metadata)
            if self.recommended_action
            else None,
            "confidence": self.confidence,
            "risk": self.risk,
            "justification": self.justification,
        }


class Reality(Protocol):
    name: str

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        ...
