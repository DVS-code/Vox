import time
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Stimulus:
    type: str
    source: str
    context: Dict[str, Any] = field(default_factory=dict)
    salience: float = 0.5
    routing: str = "ambient"  # directed | ambient | system
    timestamp: float = field(default_factory=lambda: time.time())

    def amplify(self, factor: float) -> "Stimulus":
        self.salience = max(0.0, min(1.0, self.salience * factor))
        return self

    def with_context(self, **extra: Any) -> "Stimulus":
        self.context.update(extra)
        return self
