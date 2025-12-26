import logging
import time
from collections import deque
from dataclasses import dataclass


class CircuitBreaker:
    """
    Simple circuit breaker to fail closed after repeated failures within a window.
    """

    def __init__(
        self,
        name: str,
        threshold: int = 3,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 120.0,
    ):
        self.name = name
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.failures: deque[float] = deque()
        self.tripped_until: float = 0.0
        self.reason: str = ""
        self.logger = logging.getLogger(f"vyxen.safety.{name}")

    @property
    def tripped(self) -> bool:
        return time.time() < self.tripped_until

    def allow(self) -> bool:
        now = time.time()
        self._prune(now)
        if now < self.tripped_until:
            return False
        return True

    def record_failure(self, reason: str) -> None:
        now = time.time()
        self.failures.append(now)
        self.reason = reason
        self._prune(now)
        if len(self.failures) >= self.threshold:
            self.tripped_until = now + self.cooldown_seconds
            self.logger.warning(
                "[CIRCUIT] %s tripped for %.0fs after %d failures: %s",
                self.name,
                self.cooldown_seconds,
                len(self.failures),
                reason,
            )

    def record_success(self) -> None:
        now = time.time()
        self._prune(now)
        if not self.failures:
            self.reason = ""

    def _prune(self, now: float) -> None:
        window = self.window_seconds
        while self.failures and now - self.failures[0] > window:
            self.failures.popleft()
        if self.tripped and now >= self.tripped_until:
            self.failures.clear()
            self.reason = ""


@dataclass
class SafetyDiagnostics:
    last_overrun_reason: str = ""
    last_watchdog_reason: str = ""
    log_ingest_disabled: bool = False
    tool_breaker_reason: str = ""
    memory_breaker_reason: str = ""
    llm_breaker_reason: str = ""
