import gzip
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque, Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import RuntimeConfig
from .safety import CircuitBreaker
from .stimuli import Stimulus


@dataclass
class MemoryEntry:
    id: int
    server_id: str
    stimulus_type: str
    context: Dict[str, Any]
    interpretations: Dict[str, Any]
    decision: str
    action: Dict[str, Any]
    outcome: Dict[str, Any]
    confidence_delta: float
    timestamp: float


PROFILE_DEFAULTS = {
    "verbosity": 0.5,
    "humor_tolerance": 0.5,
    "tone_balance": 0.5,
    "success_rate": 0.5,
    "warmth": 0.5,
    "formality": 0.5,
    "brevity_bias": 0.5,
    "precision": 0.5,
}

RELATIONSHIP_DEFAULTS = {
    "affinity": 0.5,
    "trust": 0.5,
    "topic_overlap": 0.5,
}


def clamp01(val: float) -> float:
    return max(0.0, min(1.0, val))


def extract_topics(text: str, max_topics: int = 5) -> List[str]:
    tokens = re.findall(r"[a-zA-Z]{4,}", text.lower())
    seen: List[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
        if len(seen) >= max_topics:
            break
    return seen


class CausalMemory:
    def __init__(self, config: RuntimeConfig, allow_writes: bool = True):
        self.config = config
        self.config.ensure_paths()
        self.allow_writes = allow_writes
        self._table_limits = config.get_memory_table_limits()
        self._write_timestamps: deque[float] = deque()
        self._breaker = CircuitBreaker("memory", threshold=5, window_seconds=60.0, cooldown_seconds=180.0)
        self.logger = logging.getLogger("vyxen.memory")
        self.warm_archive_path: Path = config.warm_archive_path
        self.disabled_due_to_size = False
        self.disabled_reason = ""
        self.last_rotation_ts: float | None = None
        self._check_size_limit()
        self._init_db()

    def _check_size_limit(self) -> None:
        try:
            size_bytes = self.config.memory_path.stat().st_size
            size_mb = size_bytes / (1024 * 1024)
            if size_mb > self.config.memory_max_file_mb:
                self.disabled_due_to_size = True
                self.allow_writes = False
                self.disabled_reason = (
                    f"memory db {size_mb:.1f}MB exceeds limit {self.config.memory_max_file_mb}MB; memory access disabled"
                )
                self.logger.warning(self.disabled_reason)
            else:
                # If size recovers, allow_writes stays whatever was requested at init
                self.disabled_due_to_size = False
                self.disabled_reason = ""
        except FileNotFoundError:
            return
        except Exception as exc:
            self.logger.warning("Memory size check failed: %s", exc)

    def _init_db(self) -> None:
        if self.disabled_due_to_size:
            return
        try:
            conn = self._open_conn()
        except Exception:
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    stimulus_type TEXT NOT NULL,
                    context TEXT NOT NULL,
                    interpretations TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    confidence_delta REAL NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            # Indexes for fast recent/echo lookups and pruning.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_server_ts ON memory(server_id, ts DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_server_type_ts ON memory(server_id, stimulus_type, ts DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    server_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY (server_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationships (
                    server_id TEXT NOT NULL,
                    user_a TEXT NOT NULL,
                    user_b TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY (server_id, user_a, user_b)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_context (
                    server_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    data TEXT NOT NULL,
                    weight REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    PRIMARY KEY (server_id, topic)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_important (
                    server_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    weight REAL NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY (server_id, user_id, key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS server_profiles (
                    server_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS server_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    action TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    created REAL NOT NULL
                )
                """
            )
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")
            conn.commit()
        except Exception as exc:
            self.logger.warning("Memory init failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _open_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)

    def _prune_writes(self, now: float | None = None) -> None:
        now = now or time.time()
        window = 1.0
        while self._write_timestamps and now - self._write_timestamps[0] > window:
            self._write_timestamps.popleft()

    def _table_over_limit(self, conn: sqlite3.Connection, table: str) -> bool:
        limit = self._table_limits.get(table)
        if not limit:
            return False
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = cur.fetchone()
            return row is not None and row[0] >= limit
        except Exception:
            return False

    def _enforce_table_limits(self) -> None:
        """
        Hard trim oversized tables to keep HOT memory bounded.
        """
        if self.disabled_due_to_size:
            return
        try:
            conn = self._open_conn()
        except Exception:
            return
        try:
            # Memory table: keep most recent hot_memory_row_cap
            cur = conn.execute("SELECT COUNT(*) FROM memory")
            total = cur.fetchone()[0]
            cap = self.config.hot_memory_row_cap
            if total > cap:
                to_delete = max(0, total - cap)
                conn.execute(
                    f"DELETE FROM memory WHERE id IN (SELECT id FROM memory ORDER BY ts ASC LIMIT ?)",
                    (to_delete,),
                )
            # Enforce per-table limits for profiles/relationships/events
            trims = [
                ("user_profiles", "updated"),
                ("relationships", "updated"),
                ("shared_context", "last_ts"),
                ("user_important", "updated"),
                ("server_events", "ts"),
                ("admin_policies", "created"),
            ]
            for table, col in trims:
                limit = self._table_limits.get(table)
                if not limit:
                    continue
                cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                if count > limit:
                    drop = count - limit
                    conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY {col} ASC LIMIT ?)",
                        (drop,),
                    )
            conn.commit()
        except Exception as exc:
            self.logger.warning("Table trim failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _execute_write(self, table: str, writer) -> None:
        if not self.allow_writes:
            return
        if not self._breaker.allow():
            return
        now = time.time()
        self._prune_writes(now)
        if len(self._write_timestamps) >= self.config.memory_max_writes_per_second:
            return
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_conn()
            if self._table_over_limit(conn, table):
                self.logger.warning("Memory limit reached for table %s; skipping write", table)
                return
            writer(conn)
            conn.commit()
            self._write_timestamps.append(time.time())
            self._breaker.record_success()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                self.logger.warning("Memory write skipped (%s): %s", table, exc)
            else:
                self._breaker.record_failure(str(exc))
        except Exception as exc:
            self._breaker.record_failure(str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ---------------------------------------------
    # Maintenance and rotation
    # ---------------------------------------------
    def maintain(self) -> Dict[str, Any]:
        """
        Perform lightweight maintenance outside cognition ticks:
        - enforce table limits
        - rotate old memory rows to warm archive
        - incremental vacuum to reclaim freed pages
        """
        report: Dict[str, Any] = {"rotated": 0, "vacuumed": False, "disabled": self.disabled_due_to_size}
        self._check_size_limit()
        if self.disabled_due_to_size:
            report["disabled_reason"] = self.disabled_reason
            return report

        self._enforce_table_limits()
        rotated = self._rotate_old_records()
        report["rotated"] = rotated

        # Incremental vacuum to avoid page bloat; keep work small
        try:
            conn = self._open_conn()
            conn.execute("PRAGMA incremental_vacuum(200);")
            conn.close()
            report["vacuumed"] = True
        except Exception:
            report["vacuumed"] = False
        return report

    def _rotate_old_records(self) -> int:
        """
        Move oldest records beyond hot cap into warm archive (summarized).
        """
        if not self.allow_writes:
            return 0
        cap = self.config.hot_memory_row_cap
        chunk = self.config.hot_rotation_chunk
        try:
            conn = self._open_conn()
            cur = conn.execute("SELECT COUNT(*) FROM memory")
            total = cur.fetchone()[0]
            if total <= cap:
                conn.close()
                return 0
            to_move = min(chunk, total - cap)
            rows = conn.execute(
                "SELECT id, server_id, stimulus_type, decision, outcome, ts FROM memory ORDER BY ts ASC LIMIT ?",
                (to_move,),
            ).fetchall()
            ids = [row[0] for row in rows]
            summaries = self._summarize_rows(rows)
            self._append_warm_archive(summaries)
            conn.execute(
                f"DELETE FROM memory WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            conn.commit()
            conn.close()
            self.last_rotation_ts = time.time()
            return len(rows)
        except Exception as exc:
            self.logger.warning("Rotation failed: %s", exc)
            return 0

    def _summarize_rows(self, rows: List[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
        """
        Build lightweight summaries for warm archive to avoid raw message logs.
        """
        summaries: List[Dict[str, Any]] = []
        type_counter: Counter[str] = Counter()
        for _, server_id, stim_type, decision, outcome, ts in rows:
            type_counter[stim_type] += 1
            summaries.append(
                {
                    "server_id": server_id,
                    "type": stim_type,
                    "decision": decision,
                    "outcome": outcome,
                    "ts": ts,
                }
            )
        # Include aggregate counts to aid future analysis without heavy reads
        if summaries:
            summaries.append(
                {
                    "summary": True,
                    "counts": dict(type_counter),
                    "ts_range": [min(r["ts"] for r in summaries if "ts" in r), max(r["ts"] for r in summaries if "ts" in r)],
                }
            )
        return summaries

    def _append_warm_archive(self, summaries: List[Dict[str, Any]]) -> None:
        if not summaries:
            return
        try:
            self.warm_archive_path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(self.warm_archive_path, mode="at", encoding="utf-8") as fh:
                for summary in summaries:
                    fh.write(json.dumps(summary) + "\n")
        except Exception as exc:
            self.logger.warning("Warm archive append failed: %s", exc)

    def size_info(self) -> Dict[str, Any]:
        hot_mb = 0.0
        warm_mb = 0.0
        try:
            hot_mb = self.config.memory_path.stat().st_size / (1024 * 1024)
        except Exception:
            pass
        try:
            warm_mb = self.warm_archive_path.stat().st_size / (1024 * 1024)
        except Exception:
            pass
        return {
            "hot_mb": hot_mb,
            "warm_mb": warm_mb,
            "last_rotation_ts": self.last_rotation_ts,
            "disabled": self.disabled_due_to_size,
            "disabled_reason": self.disabled_reason,
        }

    # ---------------------------------------------
    # Causal event log
    # ---------------------------------------------
    def record(
        self,
        server_id: str,
        stimulus: Stimulus,
        interpretations: Dict[str, Any],
        decision: str,
        action: Dict[str, Any],
        outcome: Dict[str, Any],
        confidence_delta: float,
    ) -> None:
        def writer(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO memory (
                    server_id, stimulus_type, context, interpretations,
                    decision, action, outcome, confidence_delta, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server_id,
                    stimulus.type,
                    json.dumps(stimulus.context),
                    json.dumps(interpretations),
                    decision,
                    json.dumps(action),
                    json.dumps(outcome),
                    confidence_delta,
                    stimulus.timestamp,
                ),
            )
            conn.execute(
                """
                DELETE FROM memory WHERE id NOT IN (
                    SELECT id FROM memory ORDER BY ts DESC LIMIT ?
                )
                """,
                (self.config.memory_retention_limit,),
            )
            # Time-based window to keep hot memory recent
            cutoff = time.time() - self.config.memory_retention_window_seconds
            conn.execute("DELETE FROM memory WHERE ts < ?", (cutoff,))

        self._execute_write("memory", writer)

    def fetch_recent(self, server_id: str, limit: int = 10) -> List[MemoryEntry]:
        if self.disabled_due_to_size:
            return []
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            """
            SELECT id, stimulus_type, context, interpretations, decision,
                   action, outcome, confidence_delta, ts
            FROM memory
            WHERE server_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (server_id, limit),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            MemoryEntry(
                id=row[0],
                server_id=server_id,
                stimulus_type=row[1],
                context=json.loads(row[2]),
                interpretations=json.loads(row[3]),
                decision=row[4],
                action=json.loads(row[5]),
                outcome=json.loads(row[6]),
                confidence_delta=row[7],
                timestamp=row[8],
            )
            for row in rows
        ]

    def echoes(self, server_id: str, stimulus: Stimulus, limit: int = 3) -> List[Stimulus]:
        """
        Return past stimuli of similar type for contextual echoes.
        """
        if self.disabled_due_to_size:
            return []
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            """
            SELECT stimulus_type, context, ts FROM memory
            WHERE server_id = ?
            AND stimulus_type = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (server_id, stimulus.type, limit),
        )
        rows = cur.fetchall()
        conn.close()
        echoes: List[Stimulus] = []
        now = time.time()
        for stim_type, context_json, ts in rows:
            age_hours = max(0.1, (now - ts) / 3600)
            decay = 1 / (1 + age_hours / 24)
            echoes.append(
                Stimulus(
                    type=stim_type,
                    source="memory",
                    context=json.loads(context_json),
                    salience=min(1.0, stimulus.salience * 0.7 * decay),
                    routing="system",
                    timestamp=ts,
                )
            )
        return echoes

    # ---------------------------------------------
    # Social memory utilities
    # ---------------------------------------------
    def get_user_profile(self, server_id: str, user_id: str) -> Dict[str, float]:
        if self.disabled_due_to_size:
            return PROFILE_DEFAULTS.copy()
        server_id = server_id or "global"
        data = PROFILE_DEFAULTS.copy()
        try:
            conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
            cur = conn.execute(
                "SELECT data FROM user_profiles WHERE server_id = ? AND user_id = ?",
                (server_id, str(user_id)),
            )
            row = cur.fetchone()
            conn.close()
            if row is None:
                return data
            return {k: float(v) for k, v in json.loads(row[0]).items()}
        except Exception:
            return data

    def adjust_user_profile(
        self, server_id: str, user_id: str, deltas: Dict[str, float]
    ) -> Dict[str, float]:
        server_id = server_id or "global"
        current = self.get_user_profile(server_id, user_id)
        for key, delta in deltas.items():
            if key not in current:
                continue
            current[key] = clamp01(current[key] + delta)
        self._execute_write(
            "user_profiles",
            lambda conn: conn.execute(
                "REPLACE INTO user_profiles (server_id, user_id, data, updated) VALUES (?, ?, ?, ?)",
                (server_id, str(user_id), json.dumps(current), time.time()),
            ),
        )
        return current

    def get_relationship(
        self, server_id: str, user_a: str, user_b: str
    ) -> Dict[str, float]:
        if self.disabled_due_to_size:
            return RELATIONSHIP_DEFAULTS.copy()
        server_id = server_id or "global"
        key_a, key_b = sorted([str(user_a), str(user_b)])
        data = RELATIONSHIP_DEFAULTS.copy()
        try:
            conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
            cur = conn.execute(
                "SELECT data FROM relationships WHERE server_id = ? AND user_a = ? AND user_b = ?",
                (server_id, key_a, key_b),
            )
            row = cur.fetchone()
            conn.close()
            if row is None:
                return data
            return {k: float(v) for k, v in json.loads(row[0]).items()}
        except Exception:
            return data

    def update_relationship(
        self, server_id: str, user_a: str, user_b: str, deltas: Dict[str, float]
    ) -> Dict[str, float]:
        server_id = server_id or "global"
        if user_a == user_b:
            return RELATIONSHIP_DEFAULTS.copy()
        current = self.get_relationship(server_id, user_a, user_b)
        for key, delta in deltas.items():
            if key not in current:
                continue
            current[key] = clamp01(current[key] + delta)
        key_a, key_b = sorted([str(user_a), str(user_b)])
        self._execute_write(
            "relationships",
            lambda conn: conn.execute(
                "REPLACE INTO relationships (server_id, user_a, user_b, data, updated) VALUES (?, ?, ?, ?, ?)",
                (server_id, key_a, key_b, json.dumps(current), time.time()),
            ),
        )
        return current

    def record_shared_context(
        self,
        server_id: str,
        topics: List[str],
        participants: List[str],
        weight: float,
    ) -> None:
        if self.disabled_due_to_size:
            return
        if not topics:
            return
        server_id = server_id or "global"
        now = time.time()
        if not self.allow_writes or not self._breaker.allow():
            return

        def writer(conn: sqlite3.Connection) -> None:
            for topic in topics[:5]:
                cur = conn.execute(
                    "SELECT data, weight, last_ts FROM shared_context WHERE server_id = ? AND topic = ?",
                    (server_id, topic),
                )
                row = cur.fetchone()
                if row:
                    existing_data = json.loads(row[0])
                    existing_participants = set(existing_data.get("participants", []))
                    existing_participants.update(participants)
                    new_weight = clamp01(row[1] * 0.9 + weight)
                    conn.execute(
                        """
                        UPDATE shared_context
                        SET data = ?, weight = ?, last_ts = ?
                        WHERE server_id = ? AND topic = ?
                        """,
                        (
                            json.dumps({"participants": list(existing_participants)}),
                            new_weight,
                            now,
                            server_id,
                            topic,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO shared_context (server_id, topic, data, weight, last_ts)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            server_id,
                            topic,
                            json.dumps({"participants": list(set(participants))}),
                            clamp01(weight),
                            now,
                        ),
                    )

        self._execute_write("shared_context", writer)

    def fetch_shared_context(
        self, server_id: str, topics: List[str], limit: int = 3
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        if self.disabled_due_to_size:
            return []
        if not topics:
            return []
        server_id = server_id or "global"
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        placeholders = ",".join("?" for _ in topics)
        cur = conn.execute(
            f"""
            SELECT topic, data, weight FROM shared_context
            WHERE server_id = ?
            AND topic IN ({placeholders})
            ORDER BY weight DESC
            LIMIT ?
            """,
            [server_id, *topics, limit],
        )
        rows = cur.fetchall()
        conn.close()
        return [(row[0], json.loads(row[1]), row[2]) for row in rows]

    # ---------------------------------------------
    # Important user memory (persistent)
    # ---------------------------------------------
    def save_important(
        self, server_id: str, user_id: str, key: str, value: str, weight: float
    ) -> None:
        if self.disabled_due_to_size:
            return
        server_id = server_id or "global"
        self._execute_write(
            "user_important",
            lambda conn: conn.execute(
                """
                REPLACE INTO user_important (server_id, user_id, key, value, weight, updated)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (server_id, str(user_id), key, value, clamp01(weight), time.time()),
            ),
        )

    def get_important(self, server_id: str, user_id: str) -> Dict[str, Dict[str, float]]:
        if self.disabled_due_to_size:
            return {}
        server_id = server_id or "global"
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            "SELECT key, value, weight FROM user_important WHERE server_id = ? AND user_id = ?",
            (server_id, str(user_id)),
        )
        rows = cur.fetchall()
        conn.close()
        return {row[0]: {"value": row[1], "weight": row[2]} for row in rows}

    # ---------------------------------------------
    # Server intelligence
    # ---------------------------------------------
    def record_server_snapshot(self, server_id: str, snapshot: Dict[str, Any]) -> None:
        if self.disabled_due_to_size:
            return
        server_id = server_id or "global"
        role_count = len(snapshot.get("roles", []))
        channel_count = len(snapshot.get("channels", []))
        member_count = snapshot.get("member_count")
        summary = {
            "role_count": role_count,
            "channel_count": channel_count,
            "member_count": member_count,
            "seen_at": time.time(),
        }
        self._execute_write(
            "server_profiles",
            lambda conn: conn.execute(
                "REPLACE INTO server_profiles (server_id, data, updated) VALUES (?, ?, ?)",
                (server_id, json.dumps(summary), time.time()),
            ),
        )

    def record_server_event(self, server_id: str, event_type: str, data: Dict[str, Any]) -> None:
        if self.disabled_due_to_size:
            return
        server_id = server_id or "global"
        # Store only a lightweight delta summary
        delta = {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))}
        delta["keys"] = list(data.keys())
        self._execute_write(
            "server_events",
            lambda conn: conn.execute(
                """
                INSERT INTO server_events (server_id, event_type, data, ts)
                VALUES (?, ?, ?, ?)
                """,
                (server_id, event_type, json.dumps(delta), time.time()),
            ),
        )

    def get_server_profile(self, server_id: str) -> Dict[str, Any]:
        if self.disabled_due_to_size:
            return {}
        server_id = server_id or "global"
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            "SELECT data FROM server_profiles WHERE server_id = ?", (server_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return {}
        return json.loads(row[0])

    def recent_server_events(self, server_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self.disabled_due_to_size:
            return []
        server_id = server_id or "global"
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            """
            SELECT event_type, data, ts FROM server_events
            WHERE server_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (server_id, limit),
        )
        rows = cur.fetchall()
        conn.close()
        return [{"type": r[0], "data": json.loads(r[1]), "ts": r[2]} for r in rows]

    def record_session_summary(
        self,
        server_id: str,
        session_context: Dict[str, Any],
        outcome_score: float,
        decision: str,
    ) -> None:
        if self.disabled_due_to_size:
            return
        now = time.time()
        stim = Stimulus(
            type="session_end",
            source="vyxen_core",
            context=session_context,
            salience=min(1.0, max(0.1, outcome_score)),
            routing="system",
            timestamp=now,
        )
        self.record(
            server_id=server_id,
            stimulus=stim,
            interpretations={"session": session_context},
            decision=decision,
            action={"type": "session_end"},
            outcome={"score": outcome_score},
            confidence_delta=outcome_score,
        )

    # ---------------------------------------------
    # Admin policy memory (A.D.M.I.N)
    # ---------------------------------------------
    def add_admin_policy(
        self, server_id: str, author_id: str, policy: str, action: str, condition: str
    ) -> None:
        if self.disabled_due_to_size:
            return
        self._execute_write(
            "admin_policies",
            lambda conn: conn.execute(
                """
                INSERT INTO admin_policies (server_id, author_id, policy, action, condition, created)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (server_id or "global", str(author_id), policy, action, condition, time.time()),
            ),
        )

    def fetch_admin_policies(self, server_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.config.memory_path, timeout=self.config.memory_lock_timeout)
        cur = conn.execute(
            """
            SELECT policy, action, condition, author_id, created FROM admin_policies
            WHERE server_id = ?
            ORDER BY created DESC
            LIMIT ?
            """,
            (server_id or "global", limit),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "policy": r[0],
                "action": r[1],
                "condition": r[2],
                "author_id": r[3],
                "created": r[4],
            }
            for r in rows
        ]

    def breaker_status(self) -> tuple[bool, str]:
        if self.disabled_due_to_size:
            return True, self.disabled_reason or "memory disabled due to size"
        return self._breaker.tripped, self._breaker.reason
