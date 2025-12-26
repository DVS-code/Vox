import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import re

from .stimuli import Stimulus


@dataclass
class ConversationSession:
    user_id: int
    channel_id: int
    guild_id: str
    session_start: float
    last_interaction: float
    active: bool = True
    expires_at: float = 0.0
    message_count: int = 0

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


class SessionStore:
    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl_seconds = ttl_seconds
        self.sessions: Dict[Tuple[int, str, int], ConversationSession] = {}
        self.active_by_channel: Dict[Tuple[str, int], ConversationSession] = {}

    def _key(self, user_id: int, guild_id: str, channel_id: int) -> Tuple[int, str, int]:
        return (user_id, guild_id, channel_id)

    def get(self, user_id: int, guild_id: str, channel_id: int) -> Optional[ConversationSession]:
        key = self._key(user_id, guild_id, channel_id)
        session = self.sessions.get(key)
        if session and session.expired(time.time()):
            return None
        return session

    def expire_stale(self) -> List[Tuple[ConversationSession, str]]:
        now = time.time()
        ended: List[Tuple[ConversationSession, str]] = []
        for key, session in list(self.sessions.items()):
            if session.expired(now):
                ended.append((session, "timeout"))
                self.sessions.pop(key, None)
        return ended

    def route_stimulus(
        self, stimulus: Stimulus
    ) -> Tuple[str, Optional[ConversationSession], List[Tuple[ConversationSession, str]]]:
        """
        Returns routing label, active session if any, and any sessions that expired
        as a result of this check.
        """
        now = time.time()
        ended = self.expire_stale()

        if stimulus.type not in {"discord_message", "attachment"}:
            stimulus.routing = "system"
            return stimulus.routing, None, ended

        author_id = stimulus.context.get("author_id")
        guild_id = stimulus.context.get("server_id") or "global"
        channel_id = stimulus.context.get("channel_id")
        if author_id is None or channel_id is None:
            stimulus.routing = "ambient"
            return stimulus.routing, None, ended

        mention = bool(stimulus.context.get("mentions_bot"))
        content = (stimulus.context.get("content") or "").strip().lower()
        channel_key = (guild_id, channel_id)
        active_session = self.active_by_channel.get(channel_key)

        # Clear expired active session if present
        if active_session and active_session.expired(now):
            self.sessions.pop(self._key(active_session.user_id, guild_id, channel_id), None)
            self.active_by_channel.pop(channel_key, None)
            active_session = None

        key = self._key(author_id, guild_id, channel_id)
        session = self.sessions.get(key)

        def _looks_directed(text: str) -> bool:
            if not text:
                return False
            if text.startswith(("vyxen", "vyxen,", "vyxen:", "vox", "vox,")):
                return True
            if "vyxen" in text:
                return True
            if session and not session.expired(now) and session.user_id == author_id:
                return True
            return False

        detected_direct = mention or _looks_directed(content)

        if detected_direct:
            if active_session and active_session.user_id != author_id:
                self.sessions.pop(self._key(active_session.user_id, guild_id, channel_id), None)
                self.active_by_channel.pop(channel_key, None)
                ended.append((active_session, "superseded"))
                active_session = None
            if session and not session.expired(now):
                session.last_interaction = now
                session.expires_at = now + self.ttl_seconds
                session.message_count += 1
            else:
                session = ConversationSession(
                    user_id=author_id,
                    channel_id=stimulus.context.get("channel_id"),
                    guild_id=guild_id,
                    session_start=now,
                    last_interaction=now,
                    expires_at=now + self.ttl_seconds,
                    message_count=1,
                )
                self.sessions[key] = session
            self.active_by_channel[channel_key] = session
            stimulus.routing = "directed"
            return stimulus.routing, session, ended

        if active_session and active_session.user_id == author_id:
            active_session.last_interaction = now
            active_session.expires_at = now + self.ttl_seconds
            active_session.message_count += 1
            stimulus.routing = "directed"
            return stimulus.routing, active_session, ended

        stimulus.routing = "ambient"
        return stimulus.routing, None, ended

    def end_session(self, user_id: int, guild_id: str, channel_id: int, reason: str = "disengage") -> Optional[ConversationSession]:
        key = self._key(user_id, guild_id, channel_id)
        session = self.sessions.pop(key, None)
        if session:
            session.active = False
        return session
