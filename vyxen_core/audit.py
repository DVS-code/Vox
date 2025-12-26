import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from .config import RuntimeConfig


def build_logger(config: RuntimeConfig) -> logging.Logger:
    config.ensure_paths()
    logger = logging.getLogger("vyxen.audit")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(config.audit_log_path, encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def log_decision(
    logger: logging.Logger,
    stimulus: Dict[str, Any],
    realities: List[Dict[str, Any]],
    governor_choice: Dict[str, Any],
    action_result: Dict[str, Any],
) -> None:
    payload = {
        "stimulus": stimulus,
        "realities": realities,
        "governor": governor_choice,
        "action_result": action_result,
    }
    logger.info(json.dumps(payload))
