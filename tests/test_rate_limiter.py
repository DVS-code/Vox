import time

from vyxen_core.actions import RateLimiter
from vyxen_core.config import RuntimeConfig


def test_rate_limiter_blocks_burst_and_recovers():
    config = RuntimeConfig(max_actions_per_minute=10, action_burst=2)
    limiter = RateLimiter(config)
    key = "send:channel"

    assert limiter.allow(key)
    assert limiter.allow(key)
    assert not limiter.allow(key)  # burst threshold hit

    # Simulate time passing to clear the burst window
    limiter.actions[key] = [time.time() - 10]
    assert limiter.allow(key)


def test_rate_limiter_enforces_per_minute_cap():
    config = RuntimeConfig(max_actions_per_minute=3, action_burst=5)
    limiter = RateLimiter(config)
    key = "reply:channel"

    assert limiter.allow(key)
    assert limiter.allow(key)
    assert limiter.allow(key)
    assert not limiter.allow(key)  # per-minute limit reached

    # Drop old timestamps to allow again
    limiter.actions[key] = [time.time() - 61]
    assert limiter.allow(key)
