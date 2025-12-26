import time
from typing import Dict, Optional, Tuple


class FaqStore:
    """
    Simple in-memory FAQ store keyed per guild.
    Questions are normalized to lowercase for matching.
    """

    def __init__(self, max_entries: int = 100):
        self.max_entries = max_entries
        self._faqs: Dict[str, Dict[str, Dict[str, str]]] = {}

    def _norm(self, question: str) -> str:
        return (question or "").strip().lower()

    def add(self, guild_id: str, question: str, answer: str, author_id: str) -> Tuple[str, str]:
        norm_q = self._norm(question)
        if not norm_q or not answer:
            return "", ""
        bucket = self._faqs.setdefault(str(guild_id), {})
        if len(bucket) >= self.max_entries and norm_q not in bucket:
            # Drop the oldest entry
            oldest = sorted(bucket.items(), key=lambda item: item[1].get("ts", 0))[0][0]
            bucket.pop(oldest, None)
        bucket[norm_q] = {
            "question": question.strip(),
            "answer": answer.strip(),
            "author_id": str(author_id),
            "ts": time.time(),
        }
        return question.strip(), answer.strip()

    def get(self, guild_id: str, question: str) -> Optional[str]:
        bucket = self._faqs.get(str(guild_id), {})
        entry = bucket.get(self._norm(question))
        if entry:
            return entry.get("answer")
        return None

    def list(self, guild_id: str) -> Dict[str, str]:
        bucket = self._faqs.get(str(guild_id), {})
        return {meta.get("question", q): meta.get("answer", "") for q, meta in bucket.items()}

    def remove(self, guild_id: str, question: str) -> bool:
        bucket = self._faqs.get(str(guild_id), {})
        norm_q = self._norm(question)
        if norm_q in bucket:
            bucket.pop(norm_q, None)
            return True
        return False
