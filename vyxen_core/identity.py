import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .config import RuntimeConfig


IDENTITY_TRAITS = ["assertiveness", "playfulness", "caution", "curiosity", "patience"]


@dataclass
class IdentityCore:
    config: RuntimeConfig
    values: Dict[str, float]
    allow_persistence: bool = True

    @classmethod
    def load(cls, config: RuntimeConfig, allow_persistence: bool = True) -> "IdentityCore":
        config.ensure_paths()
        values: Dict[str, float] = {}
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(config.memory_path, timeout=0.5)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_traits (
                    trait TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
                """
            )
            conn.commit()

            cur = conn.execute("SELECT trait, value FROM identity_traits")
            rows = cur.fetchall()
            if not rows:
                for trait in IDENTITY_TRAITS:
                    values[trait] = 0.5
                if allow_persistence:
                    conn.executemany(
                        "INSERT INTO identity_traits (trait, value) VALUES (?, ?)",
                        [(t, values[t]) for t in IDENTITY_TRAITS],
                    )
                    conn.commit()
            else:
                for trait, value in rows:
                    values[trait] = float(value)
        except Exception:
            # Fall back to neutral defaults if the identity store is unavailable
            values = {trait: 0.5 for trait in IDENTITY_TRAITS}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return cls(config=config, values=values, allow_persistence=allow_persistence)

    def persist(self) -> None:
        if not self.allow_persistence:
            return
        conn = sqlite3.connect(self.config.memory_path)
        conn.executemany(
            "UPDATE identity_traits SET value = ? WHERE trait = ?",
            [(self.values[t], t) for t in IDENTITY_TRAITS],
        )
        conn.commit()
        conn.close()

    def adjust_from_outcome(self, outcome_score: float) -> None:
        """
        Slowly adjust identity vector based on observed outcome score.
        Positive outcomes increase assertiveness/curiosity, negative outcomes
        increase caution/patience.
        """
        if not self.allow_persistence:
            return
        lr = self.config.identity_learning_rate
        delta = {
            "assertiveness": lr * outcome_score,
            "curiosity": lr * outcome_score,
            "playfulness": lr * outcome_score * 0.5,
            "caution": -lr * outcome_score,
            "patience": -lr * outcome_score * 0.5,
        }
        for trait, change in delta.items():
            self.values[trait] = max(0.0, min(1.0, self.values[trait] + change))
        self.persist()

    def to_json(self) -> str:
        return json.dumps(self.values)

    def summary(self) -> Dict[str, float]:
        return dict(self.values)
