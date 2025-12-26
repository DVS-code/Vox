import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import RuntimeConfig


ACTION_TYPES = {"send_message", "reply", "react", "defer", "schedule", "observe", "tool_call"}


@dataclass
class ActionIntent:
    type: str
    target_id: Optional[int]
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: time.time())

    def __post_init__(self) -> None:
        if self.type not in ACTION_TYPES:
            raise ValueError(f"Unsupported action type: {self.type}")

    def to_dict(self, include_metadata: bool = True) -> Dict[str, Any]:
        return {
            "type": self.type,
            "target_id": self.target_id,
            "payload": self.payload,
            "metadata": self.metadata if include_metadata else {k: v for k, v in self.metadata.items() if k != "audit_context"},
            "created_at": self.created_at,
        }


@dataclass
class ActionResult:
    intent: ActionIntent
    success: bool
    detail: str = ""
    executed_at: float = field(default_factory=lambda: time.time())

    def to_dict(self, include_metadata: bool = True) -> Dict[str, Any]:
        return {
            "intent": self.intent.to_dict(include_metadata=include_metadata),
            "success": self.success,
            "detail": self.detail,
            "executed_at": self.executed_at,
        }


class RateLimiter:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.window = 60.0
        self.actions: Dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        if key not in self.actions:
            self.actions[key] = []
        self.actions[key] = [t for t in self.actions[key] if now - t < self.window]
        if len(self.actions[key]) >= self.config.max_actions_per_minute:
            return False
        # Basic burst control
        recent_burst = [t for t in self.actions[key] if now - t < 5]
        if len(recent_burst) >= self.config.action_burst:
            return False
        self.actions[key].append(now)
        return True


class ActionAuditor:
    def __init__(self, max_records: int = 2000):
        self.records: deque[ActionResult] = deque(maxlen=max_records)

    def record(self, result: ActionResult) -> None:
        self.records.append(result)

    def recent_failures(self, limit: int = 5) -> list[ActionResult]:
        return [r for r in self.records if not r.success][-limit:]
