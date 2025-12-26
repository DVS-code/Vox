import gzip
import time
from pathlib import Path

from vyxen_core.config import RuntimeConfig
from vyxen_core.memory import CausalMemory
from vyxen_core.stimuli import Stimulus


def test_memory_record_rotation_and_shared_context(tmp_path: Path):
    config = RuntimeConfig(
        memory_path=tmp_path / "vyxen.db",
        warm_archive_path=tmp_path / "warm_archive.jsonl.gz",
        audit_log_path=tmp_path / "audit.log",
        memory_retention_limit=6,
        memory_retention_window_seconds=3600,
        hot_memory_row_cap=3,
        hot_rotation_chunk=2,
        memory_max_writes_per_second=20,
        memory_table_limits={
            "memory": 6,
            "user_profiles": 5,
            "relationships": 5,
            "shared_context": 5,
            "user_important": 5,
            "server_profiles": 5,
            "server_events": 5,
            "admin_policies": 5,
        },
    )
    memory = CausalMemory(config)

    base_ts = time.time()
    for idx in range(6):
        stim = Stimulus(
            type="discord_message",
            source="test",
            context={"content": f"hello {idx}"},
            salience=0.4,
            timestamp=base_ts + idx,
        )
        memory.record(
            server_id="global",
            stimulus=stim,
            interpretations={"social": {"note": idx}},
            decision="observe",
            action={"type": "observe"},
            outcome={"success": True},
            confidence_delta=0.1,
        )

    report = memory.maintain()
    recent = memory.fetch_recent("global", limit=10)
    assert len(recent) <= config.memory_retention_limit

    if report.get("rotated", 0) > 0:
        assert config.warm_archive_path.exists()
        with gzip.open(config.warm_archive_path, "rt", encoding="utf-8") as fh:
            first_line = fh.readline()
        assert first_line

    memory.record_shared_context("global", ["testing"], ["user1"], weight=0.5)
    shared = memory.fetch_shared_context("global", ["testing"])
    assert shared and shared[0][0] == "testing"
