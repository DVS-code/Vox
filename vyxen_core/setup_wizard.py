import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class WizardSession:
    guild_id: str
    user_id: str
    stage: int = 0
    started_at: float = field(default_factory=time.time)
    data: Dict[str, str] = field(default_factory=dict)


class SetupWizardStore:
    """
    Tracks simple step-by-step setup wizards per guild/user.
    This is guidance-only; no automatic admin actions are performed.
    """

    STEPS: Tuple[Tuple[str, str], ...] = (
        ("purpose", "What’s the main purpose of this server? (gaming, community, events, work)"),
        ("roles", "List the key roles you want (comma separated)."),
        ("channels", "List must-have channels (comma separated)."),
        ("moderation", "How strict should moderation be? (light / medium / strict)"),
        ("welcome_tone", "How should welcomes feel? (warm, concise, professional)"),
    )

    def __init__(self):
        self._sessions: Dict[Tuple[str, str], WizardSession] = {}

    def start(self, guild_id: str, user_id: str) -> WizardSession:
        session = WizardSession(guild_id=str(guild_id), user_id=str(user_id), stage=0)
        self._sessions[(session.guild_id, session.user_id)] = session
        return session

    def cancel(self, guild_id: str, user_id: str) -> bool:
        key = (str(guild_id), str(user_id))
        if key in self._sessions:
            self._sessions.pop(key, None)
            return True
        return False

    def active(self, guild_id: str, user_id: str) -> Optional[WizardSession]:
        return self._sessions.get((str(guild_id), str(user_id)))

    def next_prompt(self, session: WizardSession) -> Optional[str]:
        if session.stage >= len(self.STEPS):
            return None
        return self.STEPS[session.stage][1]

    def advance(self, session: WizardSession, answer: str) -> Tuple[str, bool]:
        if session.stage >= len(self.STEPS):
            return self._build_summary(session), True
        key, _ = self.STEPS[session.stage]
        session.data[key] = (answer or "").strip()
        session.stage += 1
        if session.stage >= len(self.STEPS):
            summary = self._build_summary(session)
            self._sessions.pop((session.guild_id, session.user_id), None)
            return summary, True
        prompt = self.next_prompt(session)
        return prompt or "", False

    def _build_summary(self, session: WizardSession) -> str:
        roles = [r.strip() for r in (session.data.get("roles") or "").split(",") if r.strip()]
        channels = [c.strip() for c in (session.data.get("channels") or "").split(",") if c.strip()]
        purpose = session.data.get("purpose") or "your community"
        moderation = session.data.get("moderation") or "medium"
        tone = session.data.get("welcome_tone") or "warm"

        lines = [
            f"Setup plan for {purpose}:",
            f"- Roles to create: {', '.join(roles) if roles else 'basic staff + members'}",
            f"- Channels to create: {', '.join(channels) if channels else '#general, #rules, #announcements'}",
            f"- Moderation stance: {moderation}",
            f"- Welcome tone: {tone}",
            "",
            "Suggested next steps (dry-run friendly):",
            "1) Create roles first, then channels/categories.",
            "2) Lock sensitive areas (admin/mod) before inviting members.",
            "3) Set slowmode where needed.",
            "4) Draft a welcome message and rules.",
        ]
        return "\n".join(lines)
