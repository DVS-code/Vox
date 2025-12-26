import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar, Any

T = TypeVar("T")


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    try:
        normalized = val.strip().lower()
        if not normalized:
            return default
        return normalized in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _parse(val: str | None, caster: Callable[[str], T], default: T) -> T:
    if val is None:
        return default
    try:
        return caster(val)
    except Exception:
        return default


@dataclass(frozen=True)
class RuntimeConfig:
    tick_interval_seconds: float = 0.5
    safe_mode_tick_interval_seconds: float = 1.0
    silence_gap_seconds: float = 12.0
    safe_mode_default: bool = True
    tick_budget_ms: float = 100.0
    max_tasks_per_tick: int = 6
    max_stimuli_per_tick: int = 4
    max_llm_calls_per_tick: int = 1
    stimulus_queue_limit: int = 200
    action_queue_limit: int = 50
    memory_path: Path = Path("vyxen_core/data/vyxen.db")
    memory_retention_limit: int = 2000
    hot_memory_row_cap: int = 2000
    hot_rotation_chunk: int = 400
    warm_archive_path: Path = Path("vyxen_core/data/warm_archive.jsonl.gz")
    memory_retention_window_seconds: float = 60 * 60 * 24 * 3  # keep ~3 days hot
    memory_max_file_mb: int = 600
    memory_hot_target_mb: int = 100
    memory_maintenance_interval: float = 45.0
    memory_max_writes_per_second: int = 4
    memory_table_limits: dict = None
    memory_lock_timeout: float = 0.5
    identity_learning_rate: float = 0.02
    session_ttl_seconds: float = 300.0
    audit_log_path: Path = Path("vyxen_core/data/audit.log")
    max_actions_per_minute: int = 20
    action_burst: int = 5
    pm2_log_dir: Path = Path.home() / ".pm2" / "logs"
    pm2_ingestion_enabled: bool = False
    pm2_scan_min_interval: float = 180.0
    pm2_max_lines: int = 120
    log_ingest_timeout_seconds: float = 0.25
    health_scan_interval: float = 120.0
    automod_dry_run: bool = True
    tools_enabled: bool = False
    tools_dry_run: bool = True
    admin_user_ids: tuple[str, ...] = ()
    # Treat this as "load average per CPU core" (e.g. 1.0 ~= fully saturated).
    watchdog_cpu_load: float = 1.25
    watchdog_memory_mb: int = 1024
    watchdog_event_loop_lag: float = 0.25
    watchdog_queue_depth: int = 120
    watchdog_safe_tick_interval: float = 1.5

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """
        Build a config instance with optional environment overrides. Only a subset
        of fields are exposed to keep overrides safe and explicit.
        """
        default = cls()
        return cls(
            tick_interval_seconds=_parse(os.getenv("VYXEN_TICK_INTERVAL"), float, default.tick_interval_seconds),
            safe_mode_tick_interval_seconds=_parse(
                os.getenv("VYXEN_SAFE_TICK_INTERVAL"), float, default.safe_mode_tick_interval_seconds
            ),
            silence_gap_seconds=_parse(os.getenv("VYXEN_SILENCE_GAP"), float, default.silence_gap_seconds),
            safe_mode_default=_parse_bool(os.getenv("VYXEN_SAFE_MODE_DEFAULT", ""), default.safe_mode_default),
            tick_budget_ms=_parse(os.getenv("VYXEN_TICK_BUDGET_MS"), float, default.tick_budget_ms),
            max_tasks_per_tick=_parse(os.getenv("VYXEN_MAX_TASKS_PER_TICK"), int, default.max_tasks_per_tick),
            max_stimuli_per_tick=_parse(os.getenv("VYXEN_MAX_STIMULI_PER_TICK"), int, default.max_stimuli_per_tick),
            max_llm_calls_per_tick=_parse(
                os.getenv("VYXEN_MAX_LLM_CALLS_PER_TICK"), int, default.max_llm_calls_per_tick
            ),
            stimulus_queue_limit=_parse(os.getenv("VYXEN_STIMULUS_QUEUE_LIMIT"), int, default.stimulus_queue_limit),
            action_queue_limit=_parse(os.getenv("VYXEN_ACTION_QUEUE_LIMIT"), int, default.action_queue_limit),
            memory_path=Path(os.getenv("VYXEN_MEMORY_PATH", default.memory_path)),
            memory_retention_limit=_parse(os.getenv("VYXEN_MEMORY_RETENTION_LIMIT"), int, default.memory_retention_limit),
            hot_memory_row_cap=_parse(os.getenv("VYXEN_HOT_MEMORY_ROW_CAP"), int, default.hot_memory_row_cap),
            hot_rotation_chunk=_parse(os.getenv("VYXEN_HOT_ROTATION_CHUNK"), int, default.hot_rotation_chunk),
            warm_archive_path=Path(os.getenv("VYXEN_WARM_ARCHIVE_PATH", default.warm_archive_path)),
            memory_retention_window_seconds=_parse(
                os.getenv("VYXEN_MEMORY_RETENTION_WINDOW_SECONDS"),
                float,
                default.memory_retention_window_seconds,
            ),
            memory_max_file_mb=_parse(os.getenv("VYXEN_MEMORY_MAX_FILE_MB"), int, default.memory_max_file_mb),
            memory_hot_target_mb=_parse(os.getenv("VYXEN_MEMORY_HOT_TARGET_MB"), int, default.memory_hot_target_mb),
            memory_maintenance_interval=_parse(
                os.getenv("VYXEN_MEMORY_MAINTENANCE_INTERVAL"), float, default.memory_maintenance_interval
            ),
            memory_max_writes_per_second=_parse(
                os.getenv("VYXEN_MEMORY_MAX_WRITES_PER_SECOND"), int, default.memory_max_writes_per_second
            ),
            memory_table_limits=default.memory_table_limits,
            memory_lock_timeout=_parse(os.getenv("VYXEN_MEMORY_LOCK_TIMEOUT"), float, default.memory_lock_timeout),
            identity_learning_rate=_parse(os.getenv("VYXEN_IDENTITY_LEARNING_RATE"), float, default.identity_learning_rate),
            session_ttl_seconds=_parse(os.getenv("VYXEN_SESSION_TTL_SECONDS"), float, default.session_ttl_seconds),
            audit_log_path=Path(os.getenv("VYXEN_AUDIT_LOG_PATH", default.audit_log_path)),
            max_actions_per_minute=_parse(os.getenv("VYXEN_MAX_ACTIONS_PER_MINUTE"), int, default.max_actions_per_minute),
            action_burst=_parse(os.getenv("VYXEN_ACTION_BURST"), int, default.action_burst),
            pm2_log_dir=Path(os.getenv("VYXEN_PM2_LOG_DIR", default.pm2_log_dir)),
            pm2_ingestion_enabled=_parse_bool(
                os.getenv("VYXEN_PM2_INGESTION_ENABLED", ""), default.pm2_ingestion_enabled
            ),
            pm2_scan_min_interval=_parse(os.getenv("VYXEN_PM2_SCAN_MIN_INTERVAL"), float, default.pm2_scan_min_interval),
            pm2_max_lines=_parse(os.getenv("VYXEN_PM2_MAX_LINES"), int, default.pm2_max_lines),
            log_ingest_timeout_seconds=_parse(
                os.getenv("VYXEN_LOG_INGEST_TIMEOUT_SECONDS"), float, default.log_ingest_timeout_seconds
            ),
            health_scan_interval=_parse(os.getenv("VYXEN_HEALTH_SCAN_INTERVAL"), float, default.health_scan_interval),
            automod_dry_run=_parse_bool(os.getenv("VYXEN_AUTOMOD_DRY_RUN", ""), default.automod_dry_run),
            tools_enabled=_parse_bool(os.getenv("VYXEN_TOOLS_ENABLED", ""), default.tools_enabled),
            tools_dry_run=_parse_bool(os.getenv("VYXEN_TOOLS_DRY_RUN", ""), default.tools_dry_run),
            admin_user_ids=tuple(
                uid.strip() for uid in os.getenv("VYXEN_ADMIN_USERS", "").split(",") if uid.strip()
            )
            or default.admin_user_ids,
            watchdog_cpu_load=_parse(os.getenv("VYXEN_WATCHDOG_CPU_LOAD"), float, default.watchdog_cpu_load),
            watchdog_memory_mb=_parse(os.getenv("VYXEN_WATCHDOG_MEMORY_MB"), int, default.watchdog_memory_mb),
            watchdog_event_loop_lag=_parse(
                os.getenv("VYXEN_WATCHDOG_EVENT_LOOP_LAG"), float, default.watchdog_event_loop_lag
            ),
            watchdog_queue_depth=_parse(os.getenv("VYXEN_WATCHDOG_QUEUE_DEPTH"), int, default.watchdog_queue_depth),
            watchdog_safe_tick_interval=_parse(
                os.getenv("VYXEN_WATCHDOG_SAFE_TICK_INTERVAL"), float, default.watchdog_safe_tick_interval
            ),
        )

    def ensure_paths(self) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def get_memory_table_limits(self) -> dict:
        """
        Provide per-table limits to avoid unbounded growth. Defaults are set
        conservatively to prevent disk exhaustion while retaining recent context.
        """
        if self.memory_table_limits is not None:
            return self.memory_table_limits
        return {
            "memory": 4000,
            "user_profiles": 2000,
            "relationships": 4000,
            "shared_context": 2000,
            "user_important": 2000,
            "server_profiles": 500,
            "server_events": 2000,
            "admin_policies": 500,
            "identity_traits": 20,
        }
