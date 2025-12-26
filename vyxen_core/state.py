import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class InternalState:
    social_energy: float = 0.6
    risk_pressure: float = 0.3
    narrative_load: float = 0.4
    focus: float = 0.5
    last_perceived: float = time.time()
    last_channel_id: int | None = None
    last_server_id: str | None = None
    safe_mode: bool = True
    llm_calls_remaining: int = 0
    watchdog_note: str = ""
    overrun_note: str = ""
    start_time: float = field(default_factory=time.monotonic)
    status_snapshot: Dict[str, Any] = field(default_factory=dict)
    memory_last_rotation: float | None = None
    memory_hot_mb: float = 0.0
    memory_warm_mb: float = 0.0
    memory_disabled_reason: str = ""

    def decay(self, dt: float) -> None:
        decay_rate = 0.05 * dt
        self.social_energy = max(0.0, self.social_energy - decay_rate)
        self.risk_pressure = max(0.0, self.risk_pressure - decay_rate / 2)
        self.narrative_load = max(0.0, self.narrative_load - decay_rate / 3)

    def reinforce(self, gains: Dict[str, float]) -> None:
        for key, delta in gains.items():
            if hasattr(self, key):
                setattr(self, key, max(0.0, min(1.0, getattr(self, key) + delta)))

    def update_on_stimulus(self, stim_type: str, salience: float) -> None:
        self.last_perceived = time.time()
        if stim_type == "discord_message":
            self.social_energy = min(1.0, self.social_energy + 0.05 * salience)
            self.focus = min(1.0, self.focus + 0.02 * salience)
        elif stim_type == "silence":
            self.social_energy = max(0.0, self.social_energy - 0.02 * salience)
            self.focus = max(0.0, self.focus - 0.01 * salience)
        elif stim_type.startswith("moderation"):
            self.risk_pressure = min(1.0, self.risk_pressure + 0.08 * salience)
        else:
            self.narrative_load = min(1.0, self.narrative_load + 0.03 * salience)
