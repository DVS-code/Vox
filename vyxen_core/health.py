import json
import os
import time
from pathlib import Path
from typing import Dict, List

from .config import RuntimeConfig
from .stimuli import Stimulus


def scan_pm2_logs(
    config: RuntimeConfig,
    process_name: str = "Vyxen",
    lines: int = 200,
    max_bytes: int = 50000,
    timeout_seconds: float = 0.25,
) -> List[Stimulus]:
    """
    Read PM2 error logs and emit health stimuli for cognition.
    """
    log_path = config.pm2_log_dir / f"{process_name}-error.log"
    if not log_path.exists():
        return []
    start = time.time()
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            if max_bytes > 0:
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - max_bytes))
                except Exception:
                    f.seek(0)
            content = f.readlines()
    except Exception:
        return []

    if time.time() - start > timeout_seconds:
        return []

    tail = content[-lines:] if lines else content
    errors = [line for line in tail if "Traceback" in line or "Error" in line or "exception" in line.lower()]
    if not errors:
        return []

    return [
        Stimulus(
            type="self_health",
            source="pm2",
            context={
                "log_path": str(log_path),
                "error_sample": errors[-5:],
                "count": len(errors),
            },
            salience=min(1.0, 0.2 + len(errors) * 0.05),
            routing="system",
        )
    ]
