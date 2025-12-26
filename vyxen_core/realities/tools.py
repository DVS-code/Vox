from dataclasses import dataclass
from typing import Optional

from ..actions import ActionIntent
from ..identity import IdentityCore
from ..memory import CausalMemory
from ..state import InternalState
from ..stimuli import Stimulus
from ..tool_intents import parse_natural_language_intent
from .base import RealityOutput


@dataclass
class ToolsReality:
    name: str = "ToolsReality"
    enabled: bool = False
    dry_run: bool = True

    def interpret(
        self,
        stimulus: Stimulus,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
    ) -> RealityOutput:
        if state.safe_mode or not self.enabled:
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=0.0,
                risk=0.0,
                justification="Tool execution disabled (safe mode or config).",
            )

        if stimulus.type != "discord_message":
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=0.0,
                risk=0.0,
                justification="No tool intent for non-message stimuli.",
            )

        parsed = parse_natural_language_intent(stimulus)
        if not parsed:
            # If the user is clearly asking for an admin action but we can't parse it
            # into a supported tool call yet, respond with guidance instead of going silent.
            content = (stimulus.context.get("content") or "").lower()
            verb_like = any(
                kw in content
                for kw in [
                    "create",
                    "make",
                    "set up",
                    "setup",
                    "delete",
                    "remove",
                    "ban",
                    "kick",
                    "mute",
                    "timeout",
                    "lock",
                    "restrict",
                    "hide",
                    "move",
                    "assign",
                    "give",
                    "grant",
                    "set permissions",
                    "set permission",
                ]
            )
            noun_like = any(kw in content for kw in ["role", "channel", "category", "permissions", "permission", "member", "user", "quarantine"])
            adminish_request = verb_like and noun_like
            if adminish_request:
                clarifier = ""
                if "role" in content and "@" not in content:
                    clarifier = "Which role should I change?"
                elif "channel" in content and "#" not in content:
                    clarifier = "Which channel do you mean?"
                elif "category" in content:
                    clarifier = "Which category?"
                elif "member" in content or "user" in content:
                    clarifier = "Which member should I target?"
                prompt = "I might be missing enough detail to run that."
                if clarifier:
                    prompt = f"{clarifier} {prompt}"
                return RealityOutput(
                    reality=self.name,
                    recommended_action=ActionIntent(
                        type="reply",
                        target_id=stimulus.context.get("channel_id"),
                        payload={
                            "reply_to": stimulus.context.get("message_id"),
                            "content": (
                                f"{prompt} Quote names to be sure. Examples:\n"
                                "• `create role \"Test\"`\n"
                                "• `set permissions for @Role in #channel: allow send messages`\n"
                                "• `move the \"chill-zone\" channel to the \"test\" category`\n"
                                "• `delete role \"OldRole\"` (add `confirm` to execute)\n"
                            ),
                        },
                        metadata={"reason": "unparsed_admin_request"},
                    ),
                    confidence=0.55,
                    risk=0.1,
                    justification="Admin-like request not parsed; providing guidance.",
                )
            return RealityOutput(
                reality=self.name,
                recommended_action=None,
                confidence=0.0,
                risk=0.0,
                justification="No actionable tool intent detected.",
            )

        author_perms = stimulus.context.get("author_permissions", {})
        is_admin = author_perms.get("administrator") or author_perms.get("manage_permissions") or stimulus.context.get("author_whitelisted")

        if parsed.requires_admin and not is_admin:
            # Suggest a gentle explanation instead of executing
            return RealityOutput(
                reality=self.name,
                recommended_action=ActionIntent(
                    type="reply",
                    target_id=stimulus.context.get("channel_id"),
                    payload={
                        "reply_to": stimulus.context.get("message_id"),
                        "content": (
                            "I can help with that, but I’ll only do admin changes for authorized users. "
                            "If you want, I can explain exactly what to click/change."
                        ),
                    },
                    metadata={"reason": "insufficient_permissions"},
                ),
                confidence=0.6,
                risk=0.1,
                justification="User lacks permissions; offering guidance only.",
            )

        requested_summary = parsed.requested_changes or {}
        try:
            requested_summary = {k: v for k, v in parsed.requested_changes.items() if k not in {"permissions"}}
        except Exception:
            pass

        recommended: Optional[ActionIntent] = ActionIntent(
            type="tool_call",
            target_id=stimulus.context.get("channel_id"),
            payload={
                "intent_type": parsed.intent_type,
                "target_channel": parsed.target_channel,
                "target_role": parsed.target_role,
                "requested_changes": parsed.requested_changes,
            },
            metadata={
                "author_id": stimulus.context.get("author_id"),
                "guild_id": stimulus.context.get("server_id"),
                "reason": "admin_request",
                "dry_run": parsed.dry_run or self.dry_run,
                "request_summary": requested_summary,
            },
        )

        return RealityOutput(
            reality=self.name,
            recommended_action=recommended,
            confidence=0.5 if self.dry_run else 0.85,
            risk=0.4 if self.dry_run else 0.15,
            justification="Admin request parsed into an actionable tool call.",
        )
