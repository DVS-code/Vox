import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..actions import ActionIntent
from ..identity import IdentityCore
from ..llm import craft_social_reply
from ..memory import CausalMemory, extract_topics
from ..state import InternalState
from ..stimuli import Stimulus
from .base import RealityOutput
from ..config import RuntimeConfig


@dataclass
class SocialReality:
    name: str = "SocialReality"
    config: RuntimeConfig | None = None
    session_replies: Dict[Tuple[str, int, int], Dict[str, any]] = field(default_factory=dict)

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        server_id = stimulus.context.get("server_id", "global")
        author_id = stimulus.context.get("author_id")
        profile = memory.get_user_profile(server_id, str(author_id)) if author_id else {}
        social_weight = (identity.values["assertiveness"] + identity.values["playfulness"]) / 2
        confidence = min(1.0, stimulus.salience * (0.6 + social_weight / 2))
        risk = max(0.0, 0.2 - social_weight * 0.1)
        if profile:
            confidence *= 0.8 + profile.get("success_rate", 0.5) * 0.4
            risk += 0.1 * (0.5 - profile.get("tone_balance", 0.5))

        important = memory.get_important(server_id, str(author_id)) if author_id else {}
        recommended: Optional[ActionIntent] = None
        if stimulus.type == "discord_message" and stimulus.routing != "directed":
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=0.2,
                risk=0.1,
                justification="Ambient message without active session.",
            )

        if stimulus.type == "discord_message":
            content = stimulus.context.get("content", "") or ""
            lowered = content.lower()
            target_id = stimulus.context.get("channel_id")
            prefers_quiet = bool((important or {}).get("quiet_mode", {}).get("value"))
            wants_detail = (important or {}).get("explanation_mode", {}).get("value")
            topics = extract_topics(content)
            shared_entries = memory.fetch_shared_context(server_id, topics)
            shared_topics = [topic for topic, _, _ in shared_entries]
            intent = self._classify_intent(lowered)
            session_key = None
            last_reply = None
            session_start = stimulus.context.get("session_start")
            if author_id is not None and target_id is not None:
                session_key = (server_id, int(author_id), int(target_id))
                last_reply = self.session_replies.get(session_key)
                if last_reply and session_start and last_reply.get("session_start") != session_start:
                    last_reply = None
            first_contact = last_reply is None

            if state.safe_mode:
                if target_id:
                    now = time.time()
                    reply = ""
                    reply_type = "intro"
                    safe_note_needed = False
                    if intent in {"status", "diagnostic"}:
                        recently_status = last_reply and last_reply.get("type") in {"status_full", "status_short"}
                        if recently_status and not self._should_repeat_status(lowered):
                            reply = self._status_brief(state)
                            reply_type = "status_short"
                        else:
                            reply = self._status_reply(state)
                            reply_type = "status_full"
                        safe_note_needed = False
                    elif intent in {"capability"}:
                        reply = self._capability_reply(state)
                        reply_type = "capability"
                        safe_note_needed = False
                    elif intent == "memory":
                        reply = self._memory_reply(content, important)
                        reply_type = "memory"
                        safe_note_needed = False
                    elif intent == "admin_help":
                        reply = self._admin_help_reply()
                        reply_type = "admin_help"
                        safe_note_needed = True
                    else:
                        if self._looks_like_admin_action_request(lowered):
                            reply = (
                                "I can do that, but I’m in Safe Mode right now so I can’t run admin actions. "
                                "If you say “exit safe mode”, then repeat the request, I’ll handle it."
                            )
                            reply_type = "safe_mode_block"
                            safe_note_needed = False
                        elif first_contact and intent == "greeting":
                            reply = "Hey, I’m here and keeping things light. How can I help?"
                            reply_type = "intro_brief"
                        elif intent == "greeting":
                            reply = self._greeting_reply(content)
                            reply_type = "greeting"
                        elif intent == "chat":
                            reply = self._safe_mode_chat(content, profile)
                            reply_type = "chat_short"
                        else:
                            reply = self._safe_mode_ack(profile, last_reply)
                            reply_type = "ack"

                    if last_reply and reply == last_reply.get("text") and intent != "status":
                        reply = self._safe_mode_ack(profile, last_reply)
                        reply_type = "ack"

                    if safe_note_needed and not (last_reply and last_reply.get("safe_note")):
                        reply = f"{self._safe_note()} {reply}".strip()

                    if session_key:
                        self.session_replies[session_key] = {
                            "type": reply_type,
                            "text": reply,
                            "ts": now,
                            "session_start": session_start,
                            "safe_note": safe_note_needed or (last_reply and last_reply.get("safe_note")),
                        }
                    try:
                        print(f"[SOCIAL] safe_mode intent={intent} reply_type={reply_type} content_len={len(reply)}")
                    except Exception:
                        pass
                    recommended = ActionIntent(
                        type="reply",
                        target_id=target_id,
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": reply,
                        },
                        metadata={"stimulus_type": stimulus.type, "safe_mode": True, "reply_type": reply_type},
                    )
                    confidence = max(confidence, 0.5 if reply_type == "status_full" else confidence)
                    risk = min(risk, 0.2)
            else:
                if prefers_quiet and not stimulus.context.get("mentions_bot", False):
                    return RealityOutput(
                        reality=self.name,
                        recommended_action=ActionIntent(type="observe", target_id=None, payload={}, metadata={"reason": "quiet_mode"}),
                        confidence=0.5,
                        risk=0.05,
                        justification="User prefers quiet mode; not replying without mention.",
                    )
                if intent in {"status", "diagnostic"}:
                    if target_id:
                        recently_status = last_reply and last_reply.get("type") in {"status_full", "status_short"}
                        if recently_status and not self._should_repeat_status(lowered):
                            text = self._status_brief(state)
                            reply_type = "status_short"
                        else:
                            text = self._status_reply(state)
                            reply_type = "status_full"
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={
                                "reply_to": stimulus.context.get("message_id"),
                                "content": text,
                            },
                            metadata={"stimulus_type": stimulus.type, "informational": True},
                        )
                        if session_key:
                            self.session_replies[session_key] = {
                                "type": reply_type,
                                "text": text,
                                "ts": time.time(),
                                "session_start": session_start,
                            }
                        confidence = max(confidence, 0.5)
                        risk = min(risk, 0.2)
                elif intent == "capability" and target_id:
                    text = self._capability_reply(state)
                    recommended = ActionIntent(
                        type="reply",
                        target_id=target_id,
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": text,
                        },
                        metadata={"stimulus_type": stimulus.type, "informational": True},
                    )
                    confidence = max(confidence, 0.5)
                    risk = min(risk, 0.2)
                elif intent == "memory" and target_id:
                    text = self._memory_reply(content, important)
                    recommended = ActionIntent(
                        type="reply",
                        target_id=target_id,
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": text,
                        },
                        metadata={"stimulus_type": stimulus.type, "informational": True},
                    )
                    confidence = max(confidence, 0.55)
                    risk = min(risk, 0.2)
                elif intent == "admin_help" and target_id:
                    text = self._admin_help_reply()
                    recommended = ActionIntent(
                        type="reply",
                        target_id=target_id,
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": text,
                        },
                        metadata={"stimulus_type": stimulus.type, "informational": True},
                    )
                    confidence = max(confidence, 0.6)
                    risk = min(risk, 0.15)
                elif content:
                    if "be quieter" in lowered or "only reply when mentioned" in lowered:
                        memory.save_important(server_id, str(author_id), "quiet_mode", True, weight=0.9)
                        reply = "Okay—I’ll stay quiet and only reply when you mention me."
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={"reply_to": stimulus.context.get("message_id"), "content": reply},
                            metadata={"stimulus_type": stimulus.type, "informational": True},
                        )
                        return RealityOutput(self.name, recommended, confidence=0.9, risk=0.05, justification="Quiet mode set")
                    if "stop being quiet" in lowered or "reply normally" in lowered or "talk normally" in lowered:
                        memory.save_important(server_id, str(author_id), "quiet_mode", False, weight=0.9)
                        reply = "Got it—I’ll reply normally again."
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={"reply_to": stimulus.context.get("message_id"), "content": reply},
                            metadata={"stimulus_type": stimulus.type, "informational": True},
                        )
                        return RealityOutput(self.name, recommended, confidence=0.9, risk=0.05, justification="Quiet mode cleared")
                    if "explain step by step" in lowered or "more detailed" in lowered:
                        memory.save_important(server_id, str(author_id), "explanation_mode", "detailed", weight=0.8)
                        reply = "Sure—I’ll explain things step by step until you tell me otherwise."
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={"reply_to": stimulus.context.get("message_id"), "content": reply},
                            metadata={"stimulus_type": stimulus.type, "informational": True},
                        )
                        return RealityOutput(self.name, recommended, confidence=0.9, risk=0.05, justification="Explanation mode set detailed")
                    if "keep it short" in lowered or "be concise" in lowered:
                        memory.save_important(server_id, str(author_id), "explanation_mode", "concise", weight=0.8)
                        reply = "Okay—I’ll keep replies concise."
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={"reply_to": stimulus.context.get("message_id"), "content": reply},
                            metadata={"stimulus_type": stimulus.type, "informational": True},
                        )
                        return RealityOutput(self.name, recommended, confidence=0.9, risk=0.05, justification="Explanation mode set concise")
                    if self._looks_like_admin_action_request(lowered):
                        if not (self.config and getattr(self.config, "tools_enabled", False)):
                            recommended = ActionIntent(
                                type="reply",
                                target_id=target_id,
                                payload={
                                    "reply_to": stimulus.context.get("message_id"),
                                    "content": (
                                        "I can help with admin changes, but tools are currently disabled on my side. "
                                        "If you want, I can explain the steps for you to do it manually."
                                    ),
                                },
                                metadata={"stimulus_type": stimulus.type, "informational": True},
                            )
                            confidence = max(confidence, 0.6)
                            risk = min(risk, 0.2)
                            return RealityOutput(
                                reality=self.name,
                                recommended_action=recommended,
                                confidence=confidence,
                                risk=risk,
                                justification="Tools disabled; providing guidance instead of executing.",
                            )
                        return RealityOutput(
                            reality=self.name,
                            recommended_action=None,
                            confidence=0.2,
                            risk=0.2,
                            justification="Deferring explicit admin action request to tool reality.",
                        )
                    if state.llm_calls_remaining <= 0:
                        recommended = ActionIntent(
                            type="observe",
                            target_id=None,
                            payload={},
                            metadata={"reason": "llm_budget_exhausted"},
                        )
                        confidence *= 0.5
                    else:
                        state.llm_calls_remaining -= 1
                        recommended = ActionIntent(
                            type="reply",
                            target_id=target_id,
                            payload={
                                "reply_to": stimulus.context.get("message_id"),
                                "content": "",
                                "user_content": content,
                            },
                            metadata={
                                "stimulus_type": stimulus.type,
                                "llm": True,
                                "author_id": author_id,
                                "server_id": server_id,
                            },
                        )
            if recommended:
                try:
                    print(f"[SOCIAL] intent={intent} recommended={recommended.type if recommended else None}")
                except Exception:
                    pass
        elif stimulus.type == "silence":
            # Silence is acceptable; avoid proactive messages unless explicitly prompted.
            recommended = ActionIntent(
                type="observe",
                target_id=None,
                payload={},
                metadata={"reason": "rest during silence"},
            )
            confidence *= 0.5
        return RealityOutput(
            reality=self.name,
            recommended_action=recommended,
            confidence=confidence,
            risk=risk,
            justification="Social alignment based on conversational salience.",
        )

    def _craft_social_reply(
        self,
        content: str,
        identity: IdentityCore,
        profile: dict,
        shared_topics: list[str],
        important: dict,
    ) -> str:
        return craft_social_reply(
            user_content=content,
            identity_values=identity.values,
            profile=profile or {},
            shared_topics=shared_topics,
            important=important,
        )

    def _safe_mode_reply(self, state: InternalState, profile: dict) -> str:
        tone = "I'm in Safe Mode, keeping things read-only."
        if profile.get("verbosity", 0.5) < 0.4:
            return f"{tone} I can still answer questions."
        return f"{tone} I can chat and explain status, but I won't run tools or change settings."

    def _capability_reply(self, state: InternalState) -> str:
        snap = state.status_snapshot or {}
        safe = bool(snap.get("safe_mode", True))
        tools_enabled = bool(snap.get("tools_enabled"))

        if safe:
            return (
                "Right now I’m in Safe Mode, so I’ll keep everything read-only. "
                "I can still chat normally, answer questions, explain what I can do, and help you phrase admin requests. "
                "If you want me to actually make server changes, say “exit safe mode”."
            )

        lines = [
            "Here’s what I can do:",
            "• Chat naturally like a normal member (and keep it calm/helpful).",
            "• Remember a few important things (like your preferred name) without storing raw chat logs.",
        ]
        if tools_enabled:
            lines.extend(
                [
                    "• Help with admin/mod tasks when you ask (and only if you’re authorized):",
                    "  - Create roles, channels, and categories",
                    "  - Move channels under categories",
                    "  - Lock a category to a role",
                    "  - Set channel permission overwrites (allow/deny specific permissions)",
                    "  - Audit a role’s permissions / show basic server stats",
                    "  - Timeout (mute) / quarantine / ban (if I have the Discord permissions)",
                ]
            )
        else:
            lines.append("• Admin tools are currently disabled on my side, but I can explain the steps to do things manually.")

        lines.append(
            "If you want an admin action, just ask directly (quotes help). Example: "
            "`create role \"Test\"` or `lock the admin category so only admin role can see it`."
        )
        return "\n".join(lines)

    def _status_reply(self, state: InternalState) -> str:
        snap = state.status_snapshot or {}
        uptime = snap.get("uptime_seconds", 0.0)
        minutes = int(uptime // 60)
        breakers = [
            snap.get("overrun"),
            snap.get("watchdog"),
            snap.get("pm2_breaker"),
            snap.get("llm_breaker"),
            snap.get("memory_breaker"),
        ]
        breaker_notes = ", ".join([b for b in breakers if b]) or "none"
        restart_count = snap.get("pm2_restart_count")
        restart_note = f"pm2 restarts: {restart_count}" if restart_count is not None else "pm2 restarts: unknown"
        hot_mb = snap.get("memory_hot_mb", 0.0)
        warm_mb = snap.get("memory_warm_mb", 0.0)
        rot_ts = snap.get("memory_last_rotation")
        mem_note = f"memory hot={hot_mb:.1f}MB warm={warm_mb:.1f}MB"
        if rot_ts:
            mem_note += " rotation active"
        return (
            f"Mode: {'Safe' if snap.get('safe_mode', True) else 'Active'}; "
            f"Uptime: {minutes}m; "
            f"Tick: {snap.get('tick_interval', 0.5):.2f}s; "
            f"Breakers: {breaker_notes}; "
            f"{restart_note}; "
            f"{mem_note}"
        )

    def _status_brief(self, state: InternalState) -> str:
        snap = state.status_snapshot or {}
        mode = "Safe" if snap.get("safe_mode", True) else "Active"
        hot_mb = snap.get("memory_hot_mb", 0.0)
        return f"{mode} Mode; hot memory {hot_mb:.1f}MB; read-only."

    def _safe_mode_ack(self, profile: dict, last_reply: dict | None = None) -> str:
        options = [
            "I’m here and listening.",
            "Still here, keeping it light.",
            "Here and paying attention.",
            "Got you—I’m right here.",
        ]
        idx = int(time.time()) % len(options)
        choice = options[idx]
        if last_reply and choice == last_reply.get("text"):
            choice = options[(idx + 1) % len(options)]
        if profile.get("verbosity", 0.5) < 0.4:
            return choice.split("—")[0]
        return choice

    def _safe_mode_chat(self, content: str, profile: dict) -> str:
        # Lightweight, deterministic chat reply in Safe Mode to avoid LLM dependency
        lowered = content.lower()
        if "how are you" in lowered or "how are u" in lowered:
            return "I’m steady and watching over things. What’s on your mind?"
        if "thank" in lowered:
            return "You’re welcome—I’ll keep it light."
        if "?" in lowered:
            return "I’m here and can talk it through."
        if len(content) < 40:
            return "I’m here, listening. Tell me more."
        return "Noted—I’m following along with you."

    def _is_capability_query(self, text: str) -> bool:
        phrases = [
            "what can you do",
            "capabilities",
            "commands",
            "allowed to do",
            "can you do",
            "what do i need to say",
            "what do i need to type",
            "how do i",
            "how can i",
        ]
        return any(p in text for p in phrases)

    def _is_status_query(self, text: str) -> bool:
        phrases = ["system status", "any issues", "why are you quiet", "status?", "status", "mode"]
        return any(p in text for p in phrases)

    def _should_repeat_status(self, text: str) -> bool:
        return "status" in text or "mode" in text or "what state" in text

    def _classify_intent(self, text: str) -> str:
        if self._is_status_query(text):
            return "status"
        if self._is_admin_help_query(text):
            return "admin_help"
        if self._is_memory_query(text):
            return "memory"
        if self._is_capability_query(text) or any(word in text for word in ["help", "commands", "features"]):
            return "capability"
        if any(word in text for word in ["diagnostic", "error", "log", "permission", "tool"]):
            return "diagnostic"
        if any(word in text for word in ["hello", "hi", "hey", "good morning", "good evening"]):
            return "greeting"
        return "chat"

    def _is_memory_query(self, text: str) -> bool:
        phrases = [
            "what is my name",
            "what's my name",
            "whats my name",
            "do you remember",
            "do you remember my",
            "what did i say",
            "what was my previous",
            "what was my last",
            "previous message",
            "last message",
            "remember my",
            "what was my favorite",
            "what's my favorite",
            "favorite car",
        ]
        return any(p in text for p in phrases)

    def _memory_reply(self, raw: str, important: dict) -> str:
        lowered = (raw or "").lower()
        preferred = ""
        try:
            preferred = str((important or {}).get("preferred_name", {}).get("value") or "").strip()
        except Exception:
            preferred = ""
        favorite_car = ""
        try:
            favorite_car = str((important or {}).get("favorite_car", {}).get("value") or "").strip()
        except Exception:
            favorite_car = ""

        if any(phrase in lowered for phrase in ["previous message", "last message", "what did i say", "what was my previous", "what was my last"]):
            if preferred:
                return (
                    f"I don’t keep raw chat logs, so I can’t quote your last message—but I do remember a few important notes. "
                    f"For example, I have you as `{preferred}`."
                )
            return (
                "I don’t keep raw chat logs, so I can’t quote your last message. "
                "I *can* remember a few important notes (like names/preferences) if you tell me what to save."
            )

        if any(phrase in lowered for phrase in ["what is my name", "what's my name", "whats my name", "my name"]):
            if preferred:
                return f"You go by `{preferred}`."
            return "I don’t have a preferred name saved for you yet—what should I call you?"

        if "favorite car" in lowered or "what was my favorite" in lowered or "what's my favorite" in lowered:
            if favorite_car:
                return f"Your favorite car (saved) is `{favorite_car}`."
            return "I don’t have your favorite car saved yet. If you tell me “my favorite car is …”, I’ll remember it."

        if "do you remember" in lowered or "remember my" in lowered:
            if preferred:
                return (
                    f"Yeah—I remember a few key notes (without storing raw logs). "
                    f"For example, I remember you go by `{preferred}`."
                )
            return (
                "I remember a few key notes (names/preferences/boundaries) and keep it summarized—I don’t store raw chat logs. "
                "If you want me to remember something, just tell me directly."
            )

        return (
            "I keep a small, summarized memory (names/preferences), and I don’t store raw chat logs. "
            "If you want me to remember something important, tell me what it is and I’ll save it."
        )

    def _safe_note(self) -> str:
        return "Heads up: I’m in Safe Mode (read-only for actions), but I’m here to chat."

    def _looks_like_admin_action_request(self, text: str) -> bool:
        verbs = [
            "create",
            "make",
            "set up",
            "setup",
            "add",
            "give",
            "grant",
            "allow",
            "deny",
            "revoke",
            "remove",
            "delete",
            "ban",
            "kick",
            "mute",
            "timeout",
            "lock",
            "restrict",
            "hide",
            "move",
            "audit",
            "tell me about",
        ]
        nouns = ["role", "channel", "category", "permissions", "permission", "member", "user", "quarantine"]
        if not any(v in text for v in verbs):
            return False
        return any(n in text for n in nouns)

    def _greeting_reply(self, raw: str) -> str:
        lowered = raw.lower()
        if "good morning" in lowered:
            return "Good morning."
        if "good evening" in lowered:
            return "Good evening."
        if "good night" in lowered:
            return "Good night."
        if any(word in lowered for word in ["hello", "hi", "hey"]):
            return "Hey."
        return "Hey—I'm here."

    def _is_admin_help_query(self, text: str) -> bool:
        if "?" not in text:
            return False
        return any(phrase in text for phrase in ["create", "make", "set up"]) and any(
            kw in text for kw in ["role", "channel", "category", "permissions"]
        )

    def _admin_help_reply(self) -> str:
        return (
            "If you want me to do admin work, just ask directly (and mention me). Examples:\n"
            "• “@Vyxen create role 'Test'”\n"
            "• “@Vyxen create category 'test' and channel 'test'”\n"
            "• “@Vyxen create category 'test', create channel 'test', create role 'test', then give the role access to the channel”\n"
            "• “@Vyxen check permissions for @Role in #channel”\n"
            "I’ll confirm what I’m about to change, then do it."
        )
