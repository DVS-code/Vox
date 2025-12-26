from vyxen_core.safety import CircuitBreaker


def test_circuit_breaker_trips_and_cools_down(monkeypatch):
    base_time = 1000.0
    monkeypatch.setattr("vyxen_core.safety.time.time", lambda: base_time)
    breaker = CircuitBreaker("test", threshold=2, window_seconds=10.0, cooldown_seconds=5.0)

    assert breaker.allow()
    breaker.record_failure("first")
    assert breaker.allow()
    breaker.record_failure("second")
    assert not breaker.allow()

    # Move past cooldown; breaker should recover
    monkeypatch.setattr("vyxen_core.safety.time.time", lambda: base_time + 6.0)
    assert breaker.allow()
