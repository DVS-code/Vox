import time
from typing import Dict, Optional


class MacroStore:
    """
    Simple in-memory macro store per guild.
    """

    def __init__(self, max_macros: int = 50):
        self.max_macros = max_macros
        self._macros: Dict[str, Dict[str, Dict[str, str]]] = {}

    def save(self, guild_id: str, name: str, command: str, author_id: str) -> None:
        guild_macros = self._macros.setdefault(str(guild_id), {})
        if len(guild_macros) >= self.max_macros and name not in guild_macros:
            # Drop oldest
            oldest = sorted(guild_macros.items(), key=lambda item: item[1].get("ts", 0))[0][0]
            guild_macros.pop(oldest, None)
        guild_macros[name.lower()] = {"command": command, "author_id": str(author_id), "ts": time.time()}

    def get(self, guild_id: str, name: str) -> Optional[str]:
        guild_macros = self._macros.get(str(guild_id), {})
        entry = guild_macros.get(name.lower())
        return entry.get("command") if entry else None

    def list(self, guild_id: str) -> Dict[str, str]:
        guild_macros = self._macros.get(str(guild_id), {})
        return {name: meta.get("command", "") for name, meta in guild_macros.items()}
