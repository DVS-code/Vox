import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .stimuli import Stimulus
from .safety import CircuitBreaker
from .discord_permissions import parse_permission_overwrites

_breaker = CircuitBreaker("intent_parser", threshold=5, window_seconds=60.0, cooldown_seconds=180.0)


@dataclass
class ParsedIntent:
    intent_type: str
    target_channel: Optional[int]
    target_role: Optional[int]
    requested_changes: Dict[str, Any]
    requires_admin: bool = True
    dry_run: bool = False


def parse_natural_language_intent(stimulus: Stimulus) -> Optional[ParsedIntent]:
    """
    Lightweight intent parser for admin-style server management requests.
    """
    if not _breaker.allow():
        return None
    if stimulus.type != "discord_message":
        return None

    try:
        content_raw = stimulus.context.get("content", "")
        content = content_raw.lower().strip()
        if not content:
            return None

        dry_run_request = False
        for prefix in ["dry run:", "dryrun:", "dry-run:" ]:
            if content.startswith(prefix):
                dry_run_request = True
                content_raw = content_raw[len(prefix):].lstrip()
                content = content[len(prefix):].lstrip()
                break
        # --------------------
        # Setup wizard (guidance only)
        # --------------------
        if "setup wizard" in content or content.startswith("server setup"):
            return ParsedIntent(
                intent_type="setup_wizard_start",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={},
                requires_admin=True,
                dry_run=dry_run_request,
            )
        if content.startswith("cancel setup") or content.startswith("stop setup"):
            return ParsedIntent(
                intent_type="setup_wizard_cancel",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={},
                requires_admin=True,
                dry_run=dry_run_request,
            )
        if stimulus.context.get("setup_wizard_active"):
            return ParsedIntent(
                intent_type="setup_wizard_progress",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={"answer": content_raw[:400]},
                requires_admin=True,
                dry_run=dry_run_request,
            )
        # --------------------
        # FAQ builder
        # --------------------
        m = re.search(r'add\s+faq\s+[\"“”\']([^\"“”\']{1,120})[\"“”\']\s*=\s*(.+)$', content_raw, flags=re.IGNORECASE)
        if not m:
            m = re.search(r'add\s+faq\s+([^=]{1,120})=\s*(.+)$', content_raw, flags=re.IGNORECASE)
        if m:
            question = (m.group(1) or "").strip()
            answer = (m.group(2) or "").strip()
            if question and answer:
                return ParsedIntent(
                    intent_type="add_faq",
                    target_channel=stimulus.context.get("channel_id"),
                    target_role=None,
                    requested_changes={"question": question[:120], "answer": answer[:800]},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )
        if content.startswith("list faqs") or content.startswith("show faqs"):
            return ParsedIntent(
                intent_type="list_faqs",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )
        m = re.search(r'(?:answer\s+faq|faq)\s+[\"“”\']?(.+?)[\"“”\']?$', content_raw, flags=re.IGNORECASE)
        if m:
            question = (m.group(1) or "").strip()
            if question:
                return ParsedIntent(
                    intent_type="answer_faq",
                    target_channel=stimulus.context.get("channel_id"),
                    target_role=None,
                    requested_changes={"question": question[:120]},
                    requires_admin=False,
                    dry_run=dry_run_request,
                )
        if content.startswith("remove faq") or content.startswith("delete faq"):
            m = re.search(r'(?:remove|delete)\s+faq\s+[\"“”\']?(.+?)[\"“”\']?$', content_raw, flags=re.IGNORECASE)
            if m:
                question = (m.group(1) or "").strip()
                if question:
                    return ParsedIntent(
                        intent_type="remove_faq",
                        target_channel=stimulus.context.get("channel_id"),
                        target_role=None,
                        requested_changes={"question": question[:120]},
                        requires_admin=True,
                        dry_run=dry_run_request,
                    )
        # --------------------
        # Welcome message drafting
        # --------------------
        if "welcome message" in content:
            focus = ""
            tone = ""
            channels = ""
            m_focus = re.search(r"welcome message(?:\s+(?:for|about)\s+(.+))?$", content_raw, flags=re.IGNORECASE)
            if m_focus:
                focus = (m_focus.group(1) or "").strip()
            m_channels = re.search(r"in\s+#?([a-zA-Z0-9_-]{1,60})", content_raw)
            if m_channels:
                channels = f"#{m_channels.group(1)}"
            if "friendly" in content or "casual" in content:
                tone = "friendly"
            elif "formal" in content:
                tone = "professional"
            return ParsedIntent(
                intent_type="draft_welcome_message",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={"focus": focus[:120], "tone": tone, "channels": channels[:120]},
                requires_admin=True,
                dry_run=dry_run_request,
            )
        # --------------------
        # Macros
        # --------------------
        if content.startswith("save macro"):
            m = re.search(r'save\s+macro\s+[\"“”\']([^\"“”\']{1,60})[\"“”\']\s*=\s*(.+)$', content_raw, flags=re.IGNORECASE)
            if m:
                name = (m.group(1) or "").strip()
                body = (m.group(2) or "").strip()
                if name and body:
                    return ParsedIntent(
                        intent_type="save_macro",
                        target_channel=stimulus.context.get("channel_id"),
                        target_role=None,
                        requested_changes={"macro_name": name[:60], "macro_body": body[:400]},
                        requires_admin=True,
                        dry_run=dry_run_request,
                    )
        if content.startswith("run macro"):
            m = re.search(r'run\s+macro\s+[\"“”\']?([^\"“”\']{1,60})[\"“”\']?', content_raw, flags=re.IGNORECASE)
            if m:
                name = (m.group(1) or "").strip()
                return ParsedIntent(
                    intent_type="run_macro",
                    target_channel=stimulus.context.get("channel_id"),
                    target_role=None,
                    requested_changes={"macro_name": name[:60]},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )
        if content.startswith("list macros"):
            return ParsedIntent(
                intent_type="list_macros",
                target_channel=stimulus.context.get("channel_id"),
                target_role=None,
                requested_changes={},
                requires_admin=True,
                dry_run=dry_run_request,
            )
        # --------------------
        # Scheduled tasks
        # --------------------
        if content.startswith("schedule"):
            # schedule <action> in <duration> or at <time>
            m_in = re.search(r"schedule\s+(.+?)\s+in\s+(\d+)\s*(s|sec|seconds|m|min|minutes|h|hours|d|days)", content_raw, flags=re.IGNORECASE)
            m_at = re.search(r"schedule\s+(.+?)\s+at\s+([0-2]?\d:\d{2}(?:\s*(?:am|pm))?)", content_raw, flags=re.IGNORECASE)
            action_text = None
            execute_at = None
            confirmed = "confirm" in content
            if m_in:
                action_text = (m_in.group(1) or "").strip()
                qty = int(m_in.group(2))
                unit = m_in.group(3).lower()
                multiplier = 1
                if unit.startswith("m"):
                    multiplier = 60
                elif unit.startswith("h"):
                    multiplier = 3600
                elif unit.startswith("d"):
                    multiplier = 86400
                execute_at = time.time() + qty * multiplier
            elif m_at:
                action_text = (m_at.group(1) or "").strip()
                timestr = (m_at.group(2) or "").strip()
                try:
                    from datetime import datetime
                    now = datetime.now()
                    parsed = datetime.strptime(timestr, "%H:%M")
                    execute_at = time.mktime(now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0).timetuple())
                except Exception:
                    execute_at = None
            if action_text and execute_at:
                return ParsedIntent(
                    intent_type="schedule_action",
                    target_channel=stimulus.context.get("channel_id"),
                    target_role=None,
                    requested_changes={"action_text": action_text[:400], "execute_at": execute_at, "confirmed": confirmed},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # Avoid treating pure "how do I..." questions as tool execution.
        # Those should be answered conversationally by SocialReality.
        questiony = any(content.startswith(prefix) for prefix in ["how do i", "what do i", "what should i", "what command"])
        if questiony and "can you" not in content and "please" not in content and "do " not in content:
            return None

        channel_mentions = stimulus.context.get("channel_mentions", [])
        role_mentions = stimulus.context.get("role_mentions", [])
        default_channel = stimulus.context.get("channel_id")

        def _cleanup_name(name: str) -> str:
            name = (name or "").strip().strip(" \"“”'")
            if not name:
                return ""
            if name.lower() in {
                "under",
                "below",
                "beneath",
                "in",
                "into",
                "inside",
                "within",
                "called",
                "named",
                "category",
                "channel",
                "role",
            }:
                return ""
            # Trim common trailing conjunctions / politeness that often appear after a name.
            lower = name.lower()
            cut = len(name)
            for sep in [" then", " and", ",", ".", " permissions", " permission", " please", " pls", " plz"]:
                idx = lower.find(sep)
                if idx != -1:
                    cut = min(cut, idx)
            name = name[:cut].strip().strip(" \"“”'")
            # Avoid returning empty or placeholder tokens.
            if name.lower() in {"permission", "permissions", "please", "pls", "plz"}:
                return ""
            return name

        def _extract_quoted(text: str) -> list[str]:
            # Supports straight + curly quotes and single quotes.
            return [
                m.group(1).strip()
                for m in re.finditer(r"[\"“”']([^\"“”']{1,80})[\"“”']", text)
                if m.group(1).strip()
            ]

        def _extract_keyword_quoted(text: str, keyword_pattern: str) -> Optional[str]:
            """
            Extract a quoted name associated with a specific keyword.
            Examples:
              - category "test"
              - text channel "general"
              - role 'Mods'
            """
            m = re.search(
                rf"{keyword_pattern}\s*[\"“”']([^\"“”']{{1,80}})[\"“”']",
                text,
                flags=re.IGNORECASE,
            )
            if not m:
                return None
            return (m.group(1) or "").strip()

        def _extract_named(text: str, keyword: str) -> Optional[str]:
            # e.g. "role called test", "channel named test"
            m = re.search(
                rf"{keyword}\s+(?:called|named)\s+([a-zA-Z0-9 _-]{{1,64}})",
                text,
                flags=re.IGNORECASE,
            )
            if not m:
                return None
            name = _cleanup_name(m.group(1))
            return name or None

        def _extract_create_pattern(text: str, keyword: str) -> Optional[str]:
            """
            Handle common shorthand like "create role test" / "create category test"
            (without requiring quotes or the word "called").
            """
            if keyword == "channel":
                pat = r"(?:create|make|add|set up|setup)\s+(?:a\s+new\s+)?(?:(?:text|voice)\s+)?channel\s+([a-zA-Z0-9 _-]{1,64})"
            else:
                pat = rf"(?:create|make|add|set up|setup)\s+(?:a\s+new\s+)?{keyword}\s+([a-zA-Z0-9 _-]{{1,64}})"
            m = re.search(pat, text, flags=re.IGNORECASE)
            if not m:
                return None
            name = _cleanup_name(m.group(1))
            return name or None

        def _extract_under_category(text: str) -> Optional[str]:
            m = re.search(
                r"\b(?:under|in|inside|within)\s+(?:the\s+)?[\"“”']([^\"“”']{1,80})[\"“”']\s+category\b",
                text,
                flags=re.IGNORECASE,
            )
            if m:
                return (m.group(1) or "").strip()
            m = re.search(
                r"\b(?:under|in|inside|within)\s+(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+category\b",
                text,
                flags=re.IGNORECASE,
            )
            if m:
                return _cleanup_name(m.group(1)) or None
            return None

        def _extract_called_after(keyword_pattern: str, text: str) -> Optional[str]:
            # Allow a small span between the keyword and "called"/"named".
            m = re.search(
                rf"{keyword_pattern}[^\\n]{{0,140}}?\\b(?:called|named)\\b\\s*[\"“”']([^\"“”']{{1,80}})[\"“”']",
                text,
                flags=re.IGNORECASE,
            )
            if m:
                return (m.group(1) or "").strip()
            m = re.search(
                rf"{keyword_pattern}[^\\n]{{0,140}}?\\b(?:called|named)\\b\\s+([a-zA-Z0-9 _-]{{1,64}})",
                text,
                flags=re.IGNORECASE,
            )
            if m:
                return _cleanup_name(m.group(1)) or None
            return None

        # --------------------
        # Role permissions report (role-level, not channel overwrites)
        # --------------------
        if (
            any(word in content for word in ["audit", "check", "show", "list"])
            and "role" in content
            and not (("channel" in content) or channel_mentions)
        ):
            role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
            if not role_name:
                m = re.search(
                    r"(?:for|of)\s+(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+role",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    role_name = _cleanup_name(m.group(1))
            if not role_name and "admin role" in content:
                role_name = "admin"
            if role_name or role_mentions:
                requested: Dict[str, Any] = {}
                if role_name:
                    requested["role_name"] = role_name.strip()[:60]
                return ParsedIntent(
                    intent_type="role_permissions_report",
                    target_channel=default_channel,
                    target_role=role_mentions[0] if role_mentions else None,
                    requested_changes=requested,
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Server stats / overview (read-only)
        # --------------------
        stats_like = any(
            phrase in content
            for phrase in [
                "server stats",
                "server statistics",
                "server overview",
                "member count",
                "channel count",
                "role count",
                "audit the server",
                "audit server",
            ]
        )
        if stats_like:
            return ParsedIntent(
                intent_type="server_stats_report",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # List roles (read-only)
        # --------------------
        roles_query = any(
            phrase in content
            for phrase in [
                "what roles",
                "list roles",
                "show roles",
                "roles in this server",
                "roles in the server",
                "server roles",
            ]
        )
        action_verbs_present = any(
            verb in content
            for verb in [
                "create",
                "make",
                "set up",
                "setup",
                "add",
                "delete",
                "remove",
                "assign",
                "give",
                "grant",
            ]
        )
        if roles_query and ("role" in content or "roles" in content) and not action_verbs_present:
            return ParsedIntent(
                intent_type="list_roles",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # List channels (read-only)
        # --------------------
        channels_query = any(
            phrase in content
            for phrase in [
                "what channels",
                "list channels",
                "show channels",
                "channels in this server",
                "channels in the server",
                "server channels",
            ]
        )
        if channels_query and "channel" in content:
            return ParsedIntent(
                intent_type="list_channels",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # Assign a role to a member
        # --------------------
        if any(verb in content for verb in ["assign", "give", "grant", "add"]) and "role" in content and any(
            hint in content for hint in [" to me", " to user", " to member", " give me", " to <@"]
        ):
            role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
            if not role_name:
                m = re.search(
                    r"\b(?:assign|give|grant|add)\b\s+(?:me\s+)?(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+role\b",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    role_name = _cleanup_name(m.group(1))

            member_id: Optional[str] = None
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            if mentioned:
                member_id = str(mentioned[0])
            if member_id is None and any(phrase in content for phrase in ["to me", "give me", "me please", "me pls"]):
                if stimulus.context.get("author_id") is not None:
                    member_id = str(stimulus.context.get("author_id"))
            if member_id is None:
                m = re.search(r"\b(\d{17,20})\b", content_raw)
                if m:
                    member_id = m.group(1)

            if (role_name or role_mentions) and member_id:
                requested: Dict[str, Any] = {"member_id": member_id}
                if role_name:
                    requested["role_name"] = role_name.strip()[:60]
                return ParsedIntent(
                    intent_type="assign_role",
                    target_channel=default_channel,
                    target_role=role_mentions[0] if role_mentions else None,
                    requested_changes=requested,
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Update guild-level role permissions (not channel overwrites)
        # --------------------
        if (
            any(verb in content for verb in ["assign", "give", "grant", "set", "add", "remove", "revoke", "take"])
            and "role" in content
            and any(word in content for word in ["permission", "permissions"])
            and not (("channel" in content) or channel_mentions)
        ):
            perm_spec = parse_permission_overwrites(content_raw).overwrites
            server_level_hints = {"administrator", "ban_members", "kick_members", "manage_guild", "view_audit_log"}
            if perm_spec and (set(perm_spec.keys()) & server_level_hints):
                role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
                if not role_name:
                    m = re.search(
                        r"(?:to|for)\s+(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+role\b",
                        content_raw,
                        flags=re.IGNORECASE,
                    )
                    if m:
                        role_name = _cleanup_name(m.group(1))
                if not role_name and "admin role" in content:
                    role_name = "admin"
                if role_name or role_mentions:
                    requested: Dict[str, Any] = {"permissions": perm_spec}
                    if role_name:
                        requested["role_name"] = role_name.strip()[:60]
                    return ParsedIntent(
                        intent_type="role_permissions_update",
                        target_channel=default_channel,
                        target_role=role_mentions[0] if role_mentions else None,
                        requested_changes=requested,
                        requires_admin=True,
                        dry_run=dry_run_request,
                    )

        # --------------------
        # Permissions check/fix (existing roles/channels)
        # --------------------
        create_like = any(verb in content for verb in ["create", "make", "set up", "setup", "add"])
        permission_like = any(word in content for word in ["permission", "permissions", "overwrite", "overwrites"])
        access_like = any(word in content for word in ["access", "allow", "deny", "grant", "give", "revoke", "remove"])
        server_object_like = bool(channel_mentions) or bool(role_mentions) or any(word in content for word in ["role", "channel", "category"])
        if (permission_like or access_like) and server_object_like and not (
            create_like and any(word in content for word in ["role", "channel", "category"])
        ):
            target_channel = channel_mentions[0] if channel_mentions else default_channel
            target_role = role_mentions[0] if role_mentions else None

            wants_update = any(word in content for word in ["fix", "update", "change", "set", "allow", "deny", "grant", "give", "revoke", "remove"])
            perm_spec = parse_permission_overwrites(content_raw).overwrites

            wants_channel_access = any(
                phrase in content
                for phrase in [
                    "access to the channel",
                    "access the channel",
                    "see the channel",
                    "see channel",
                    "view channel",
                    "view_channel",
                    "read messages",
                    "read_messages",
                ]
            )
            if wants_channel_access and not any(key in perm_spec for key in ["view_channel", "read_messages"]):
                perm_spec["view_channel"] = True

            if wants_update:
                if not perm_spec:
                    perm_spec = {"view_channel": True}
                requested_changes = {"permissions": perm_spec}
                intent_type = "permission_check_and_fix"
            else:
                requested_changes = {}
                intent_type = "permission_check"
            return ParsedIntent(
                intent_type=intent_type,
                target_channel=target_channel,
                target_role=target_role,
                requested_changes=requested_changes,
                requires_admin=True,
                dry_run=dry_run_request,
            )
        # --------------------
        # Permission explain / diff
        # --------------------
        if "why can't" in content or "why cant" in content or "why cannot" in content:
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            channel_target = channel_mentions[0] if channel_mentions else default_channel
            if mentioned:
                return ParsedIntent(
                    intent_type="permission_explain",
                    target_channel=channel_target,
                    target_role=None,
                    requested_changes={"user_id": str(mentioned[0])},
                    requires_admin=False,
                    dry_run=dry_run_request,
                )
        if "permission diff" in content or "permission difference" in content:
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            channel_target = channel_mentions[0] if channel_mentions else default_channel
            if mentioned:
                return ParsedIntent(
                    intent_type="permission_explain",
                    target_channel=channel_target,
                    target_role=None,
                    requested_changes={"user_id": str(mentioned[0])},
                    requires_admin=False,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Role impact preview
        # --------------------
        if "what would happen if i give" in content or "what happens if i give" in content or "if i give" in content:
            role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
            if not role_name and role_mentions:
                role_name = None
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            member_id = str(mentioned[0]) if mentioned else None
            if role_name is None:
                m = re.search(r"give\s+@?[^\s]+\s+([a-zA-Z0-9 _-]{1,64})", content_raw, flags=re.IGNORECASE)
                if m:
                    role_name = _cleanup_name(m.group(1))
            if role_name is None and "admin" in content:
                role_name = "admin"
            if member_id and (role_name or role_mentions):
                requested: Dict[str, Any] = {"member_id": member_id}
                if role_name:
                    requested["role_name"] = role_name.strip()[:60]
                return ParsedIntent(
                    intent_type="role_impact_preview",
                    target_channel=default_channel,
                    target_role=role_mentions[0] if role_mentions else None,
                    requested_changes=requested,
                    requires_admin=False,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Destructive/admin moderation actions
        # --------------------
        confirm = "confirm" in content

        if re.search(r"\b(?:delete|remove)\s+role\b", content):
            role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
            if not role_name:
                m = re.search(
                    r"(?:delete|remove)\s+role\s+([a-zA-Z0-9 _-]{1,64})",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    role_name = _cleanup_name(m.group(1))
            requested: Dict[str, Any] = {"confirmed": confirm}
            if role_name:
                requested["role_name"] = role_name.strip()[:60]
            return ParsedIntent(
                intent_type="delete_role",
                target_channel=default_channel,
                target_role=role_mentions[0] if role_mentions else None,
                requested_changes=requested,
                requires_admin=True,
                dry_run=dry_run_request,
            )

        if re.search(r"\bban\s+(?:member|user)\b", content) or re.search(r"\bban\b", content):
            member_id = None
            # Prefer explicit mention IDs when available.
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            if mentioned:
                member_id = str(mentioned[0])
            if member_id is None:
                m = re.search(r"\b(\d{17,20})\b", content_raw)
                if m:
                    member_id = m.group(1)
            if member_id:
                return ParsedIntent(
                    intent_type="ban_member",
                    target_channel=default_channel,
                    target_role=None,
                    requested_changes={"member_id": member_id, "confirmed": confirm},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        if re.search(r"\b(?:mute|timeout)\s+(?:member|user)\b", content) or re.search(r"\btimeout\b", content):
            member_id = None
            mentioned = stimulus.context.get("mentioned_user_ids", [])
            if mentioned:
                member_id = str(mentioned[0])
            if member_id is None:
                m = re.search(r"\b(\d{17,20})\b", content_raw)
                if m:
                    member_id = m.group(1)

            def _parse_duration_seconds(text: str) -> int:
                m = re.search(r"\b(\d{1,4})\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\b", text, flags=re.IGNORECASE)
                if not m:
                    return 600
                qty = int(m.group(1))
                unit = m.group(2).lower()
                if unit.startswith("s"):
                    return qty
                if unit.startswith("m"):
                    return qty * 60
                if unit.startswith("h"):
                    return qty * 3600
                if unit.startswith("d"):
                    return qty * 86400
                return 600

            if member_id:
                return ParsedIntent(
                    intent_type="timeout_member",
                    target_channel=default_channel,
                    target_role=None,
                    requested_changes={"member_id": member_id, "duration_seconds": _parse_duration_seconds(content_raw)},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Quarantine helper
        # --------------------
        if "quarantine" in content and any(word in content for word in ["setup", "set up", "create"]) and any(
            word in content for word in ["assign", "apply", "give"]
        ):
            m = re.search(r"\b(\d{17,20})\b", content_raw)
            if m:
                member_id = m.group(1)
                return ParsedIntent(
                    intent_type="quarantine_member",
                    target_channel=default_channel,
                    target_role=None,
                    requested_changes={
                        "member_id": member_id,
                        "category_name": "quarantine",
                        "channel_name": "quarantine",
                        "role_name": "quarantine",
                    },
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # User profile report (from Vyxen memory)
        # --------------------
        if ("tell me about" in content or "about user" in content) and "user" in content:
            m = re.search(r"\b(\d{17,20})\b", content_raw)
            if m:
                return ParsedIntent(
                    intent_type="user_profile_report",
                    target_channel=default_channel,
                    target_role=None,
                    requested_changes={"user_id": m.group(1)},
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Server activity / recent changes (read-only, no LLM)
        # --------------------
        activity_like = any(
            phrase in content
            for phrase in [
                "what changed recently",
                "recent changes",
                "what has changed",
                "what’s been going on",
                "whats been going on",
                "how active is this server",
                "server activity",
                "what happened lately",
            ]
        )
        if activity_like:
            return ParsedIntent(
                intent_type="server_activity_report",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # Channel activity heatmap
        # --------------------
        if any(
            phrase in content
            for phrase in [
                "most active channels",
                "most active channel",
                "channel activity",
                "which channels are most active",
                "activity heatmap",
            ]
        ):
            return ParsedIntent(
                intent_type="channel_activity_report",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # User activity summary
        # --------------------
        if any(phrase in content for phrase in ["how active is", "activity for", "activity of"]) and stimulus.context.get("mentioned_user_ids"):
            user_id = str(stimulus.context.get("mentioned_user_ids")[0])
            return ParsedIntent(
                intent_type="user_activity_summary",
                target_channel=default_channel,
                target_role=None,
                requested_changes={"user_id": user_id},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # Audit summaries
        # --------------------
        if any(
            phrase in content
            for phrase in [
                "summarize admin actions",
                "admin summary",
                "audit summary",
                "what did admins do today",
                "what did you do today",
            ]
        ):
            return ParsedIntent(
                intent_type="audit_summary",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )

        # --------------------
        # Last action explain/undo (read-only or explain)
        # --------------------
        if any(phrase in content for phrase in ["what did you just do", "what did you just change", "last thing you did", "explain the last thing you did"]):
            return ParsedIntent(
                intent_type="last_action_explain",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=False,
                dry_run=dry_run_request,
            )
        if "undo" in content and "last action" in content:
            return ParsedIntent(
                intent_type="undo_last_action",
                target_channel=default_channel,
                target_role=None,
                requested_changes={},
                requires_admin=True,
                dry_run=dry_run_request,
            )

        # --------------------
        # Move channel under category
        # --------------------
        move_like = any(word in content for word in ["move", "put", "place"])
        if move_like and "channel" in content and "category" in content:
            quoted = _extract_quoted(content_raw)

            category_name = _extract_keyword_quoted(content_raw, r"category") or _extract_named(content_raw, "category")
            if not category_name:
                m = re.search(
                    r"[\"“”']([^\"“”']{1,80})[\"“”']\s+category",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    category_name = (m.group(1) or "").strip()

            channel_name = _extract_keyword_quoted(
                content_raw, r"(?:(?:text|voice)\s+)?channel"
            ) or _extract_named(content_raw, "channel")
            if not channel_name:
                m = re.search(
                    r"[\"“”']([^\"“”']{1,80})[\"“”']\s+(?:(?:text|voice)\s+)?channel",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    channel_name = (m.group(1) or "").strip()

            # Last-resort: if there are exactly two quoted strings, treat them as
            # (channel, category) in that order.
            if quoted and len(quoted) == 2:
                channel_name = channel_name or quoted[0]
                category_name = category_name or quoted[1]
            requested: Dict[str, Any] = {}
            if category_name:
                requested["category_name"] = category_name.strip()[:60]
            if channel_name:
                requested["channel_name"] = channel_name.strip()[:60]
            target_channel = channel_mentions[0] if channel_mentions else default_channel
            if requested.get("category_name") and (channel_mentions or requested.get("channel_name")):
                return ParsedIntent(
                    intent_type="move_channel_to_category",
                    target_channel=target_channel,
                    target_role=None,
                    requested_changes=requested,
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Lock/restrict a category to a role
        # --------------------
        lock_like = any(word in content for word in ["lock", "restrict", "hide"])
        if lock_like and "category" in content:
            strict = any(word in content for word in ["only", "just"])
            category_name = _extract_keyword_quoted(content_raw, r"category") or _extract_named(content_raw, "category")
            if not category_name:
                m = re.search(
                    r"(?:lock|restrict|hide)\s+(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+category",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    category_name = _cleanup_name(m.group(1))
            if not category_name and "admin category" in content:
                category_name = "admin"

            role_name = _extract_keyword_quoted(content_raw, r"role") or _extract_named(content_raw, "role")
            if not role_name:
                m = re.search(
                    r"only\s+(?:the\s+)?([a-zA-Z0-9 _-]{1,64})\s+role",
                    content_raw,
                    flags=re.IGNORECASE,
                )
                if m:
                    role_name = _cleanup_name(m.group(1))
            if not role_name and "admin role" in content:
                role_name = "admin"

            if category_name and (role_name or role_mentions):
                requested: Dict[str, Any] = {
                    "category_name": category_name.strip()[:60],
                    "role_name": (role_name.strip()[:60] if role_name else None),
                    "strict": strict,
                }
                requested = {k: v for k, v in requested.items() if v is not None}
                return ParsedIntent(
                    intent_type="lock_category",
                    target_channel=default_channel,
                    target_role=role_mentions[0] if role_mentions else None,
                    requested_changes=requested,
                    requires_admin=True,
                    dry_run=dry_run_request,
                )

        # --------------------
        # Create / setup intents
        # --------------------
        create_verbs = any(verb in content for verb in ["create", "make", "set up", "setup", "add"])
        implicit_channel_create = bool(re.match(r"^(?:text|voice)\s+channel\b", content))
        if not (create_verbs or implicit_channel_create):
            return None

        quoted = _extract_quoted(content_raw)
        both_name: Optional[str] = None
        if "name them both" in content or "name both" in content:
            both_name = quoted[0] if quoted else None

        role_name = (
            _extract_named(content_raw, "role")
            or _extract_keyword_quoted(content_raw, r"role")
            or _extract_called_after(r"role", content_raw)
            or _extract_create_pattern(content_raw, "role")
        )
        channel_name = (
            _extract_named(content_raw, "channel")
            or _extract_keyword_quoted(content_raw, r"(?:(?:text|voice)\s+)?channel")
            or _extract_called_after(r"(?:(?:text|voice)\s+)?channel", content_raw)
            or _extract_create_pattern(content_raw, "channel")
        )
        category_name = (
            _extract_named(content_raw, "category")
            or _extract_keyword_quoted(content_raw, r"category")
            or _extract_create_pattern(content_raw, "category")
        )
        if category_name is None:
            category_name = _extract_under_category(content_raw)

        if both_name:
            if "category" in content:
                category_name = both_name
            if "channel" in content:
                channel_name = both_name

        # Fall back to first quoted string if a specific name wasn't found.
        if quoted:
            if role_name is None and "role" in content:
                role_name = quoted[0]
            if category_name is None and "category" in content:
                category_name = quoted[0]
            if channel_name is None and "channel" in content:
                # If we already identified the category name from a quoted string, treat
                # the remaining quoted token as the channel name.
                if category_name and quoted and len(quoted) >= 2 and category_name == quoted[0]:
                    channel_name = quoted[1]
                else:
                    channel_name = quoted[-1]

        requested: Dict[str, Any] = {}
        if category_name and "category" in content:
            requested["category_name"] = category_name.strip()[:60]
        if channel_name and "channel" in content:
            # Discord channel names are lowercase, hyphenated; let Discord normalize if needed
            requested["channel_name"] = channel_name.strip()[:60]
            if "voice channel" in content:
                requested["channel_type"] = "voice"
        if role_name and "role" in content:
            requested["role_name"] = role_name.strip()[:60]

        wants_permissions = any(
            phrase in content
            for phrase in [
                "permission",
                "permissions",
                "overwrite",
                "overwrites",
                "set permissions",
                "allow",
                "deny",
                "grant",
                "give",
                "revoke",
                "remove",
                "access",
            ]
        )
        wants_channel_access = any(
            phrase in content
            for phrase in [
                "access to the channel",
                "access the channel",
                "see the channel",
                "see channel",
                "view channel",
                "view_channel",
                "read messages",
                "read_messages",
            ]
        )

        if wants_permissions:
            perm_spec = parse_permission_overwrites(content_raw).overwrites
            if wants_channel_access and not any(key in perm_spec for key in ["view_channel", "read_messages"]):
                perm_spec["view_channel"] = True
            if perm_spec:
                requested["permissions"] = perm_spec

        # Prefer existing mentions for permission adjustments.
        target_channel = channel_mentions[0] if channel_mentions else default_channel
        target_role = role_mentions[0] if role_mentions else None

        if not requested:
            return None

        meta_keys = {"channel_type"}
        effective_keys = {k for k in requested.keys() if k not in meta_keys}

        if {"category_name", "channel_name", "role_name"}.issubset(effective_keys) and "permissions" in requested:
            intent_type = "bulk_setup"
        elif effective_keys == {"role_name"}:
            intent_type = "create_role"
        elif effective_keys == {"category_name"}:
            intent_type = "create_category"
        elif effective_keys == {"category_name", "channel_name"} and "role_name" not in requested and "permissions" not in requested:
            intent_type = "create_voice_channel" if requested.get("channel_type") == "voice" else "create_text_channel"
        elif effective_keys == {"channel_name"}:
            intent_type = "create_voice_channel" if requested.get("channel_type") == "voice" else "create_text_channel"
        else:
            intent_type = "server_setup"

        return ParsedIntent(
            intent_type=intent_type,
            target_channel=target_channel,
            target_role=target_role,
            requested_changes=requested,
            requires_admin=True,
            dry_run=dry_run_request,
        )
    except Exception as exc:
        _breaker.record_failure(str(exc))
        return None
