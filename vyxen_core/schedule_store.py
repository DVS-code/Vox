import asyncio
import time
from typing import Callable, Dict, Optional


class ScheduledTask:
    def __init__(self, task_id: str, execute_at: float, payload: dict):
        self.task_id = task_id
        self.execute_at = execute_at
        self.payload = payload
        self.created_at = time.time()
        self.cancelled = False


class ScheduleStore:
    """
    In-memory scheduler for admin tasks. Tasks are not persisted across restarts.
    """

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}

    def schedule(self, task_id: str, execute_at: float, payload: dict) -> ScheduledTask:
        entry = ScheduledTask(task_id, execute_at, payload)
        self._tasks[task_id] = entry
        return entry

    def cancel(self, task_id: str) -> bool:
        entry = self._tasks.get(task_id)
        if entry:
            entry.cancelled = True
            self._tasks.pop(task_id, None)
            return True
        return False

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        return self._tasks.get(task_id)

    def list(self) -> Dict[str, ScheduledTask]:
        return dict(self._tasks)

    async def run_task(self, entry: ScheduledTask, executor: Callable[[dict], "asyncio.Future"]):
        delay = max(0.0, entry.execute_at - time.time())
        await asyncio.sleep(delay)
        if entry.cancelled:
            return
        try:
            await executor(entry.payload)
        finally:
            self._tasks.pop(entry.task_id, None)
