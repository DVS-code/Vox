import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ActionEntry:
    user_id: str
    action_type: str
    targets: Dict[str, Any]
    before_state: Dict[str, Any] | None
    after_state: Dict[str, Any] | None
    reversible: bool
    timestamp: float = field(default_factory=time.time)


class ActionJournal:
    """
    In-memory journal of admin actions to support explain/undo.
    Keeps a bounded list per user.
    """

    def __init__(self, max_entries_per_user: int = 25):
        self.max_entries_per_user = max_entries_per_user
        self._entries: Dict[str, List[ActionEntry]] = {}

    def record(
        self,
        user_id: str,
        action_type: str,
        targets: Dict[str, Any],
        before_state: Dict[str, Any] | None,
        after_state: Dict[str, Any] | None,
        reversible: bool,
    ) -> ActionEntry:
        entry = ActionEntry(
            user_id=str(user_id),
            action_type=action_type,
            targets=targets,
            before_state=before_state,
            after_state=after_state,
            reversible=reversible,
        )
        bucket = self._entries.setdefault(str(user_id), [])
        bucket.append(entry)
        if len(bucket) > self.max_entries_per_user:
            bucket.pop(0)
        return entry

    def last(self, user_id: str) -> Optional[ActionEntry]:
        bucket = self._entries.get(str(user_id)) or []
        return bucket[-1] if bucket else None

    def last_reversible(self, user_id: str) -> Optional[ActionEntry]:
        bucket = self._entries.get(str(user_id)) or []
        for entry in reversed(bucket):
            if entry.reversible:
                return entry
        return None

    def pop_last_reversible(self, user_id: str) -> Optional[ActionEntry]:
        bucket = self._entries.get(str(user_id)) or []
        for idx in range(len(bucket) - 1, -1, -1):
            if bucket[idx].reversible:
                return bucket.pop(idx)
        return None
