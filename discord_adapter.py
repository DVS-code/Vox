import asyncio
import concurrent.futures
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
import resource

import discord
try:
    from dotenv import load_dotenv
except ImportError:
    # Minimal fallback to load a local .env file without external deps
    def load_dotenv(path: str = ".env", *args, **kwargs):  # type: ignore
        candidates = [Path(path)]
        try:
            candidates.append(Path(__file__).resolve().parent / path)
        except Exception:
            pass
        loaded = False
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, val = stripped.split("=", 1)
                    if key and val and (key not in os.environ or not os.environ.get(key)):
                        os.environ[key] = val.strip().strip('"').strip("'")
                loaded = True
            except Exception:
                continue
        return loaded

from vyxen_core import (
    ActionIntent,
    ActionResult,
    CausalMemory,
    CognitionLoop,
    Governor,
    IdentityCore,
    InternalState,
    RuntimeConfig,
    Stimulus,
    SessionStore,
)
from vyxen_core.actions import RateLimiter
from vyxen_core.audit import build_logger, log_decision
from vyxen_core.action_journal import ActionJournal, ActionEntry
from vyxen_core.macro_store import MacroStore
from vyxen_core.schedule_store import ScheduleStore
from vyxen_core.faq_store import FaqStore
from vyxen_core.setup_wizard import SetupWizardStore
from vyxen_core.safety import CircuitBreaker


load_dotenv()


class DiscordAdapter(discord.Client):
    def __init__(
        self,
        config: RuntimeConfig,
        cognition: CognitionLoop,
        stimulus_queue: asyncio.Queue,
        action_queue: asyncio.Queue,
    ):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
        intents.messages = True
        super().__init__(
            intents=intents,
            max_messages=200,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self.config = config
        self.cognition = cognition
        self.stimulus_queue = stimulus_queue
        self.action_queue = action_queue
        self.logger = build_logger(config)
        self.rate_limiter = RateLimiter(config)
        self.tool_breaker = CircuitBreaker("tool_execution", threshold=2, window_seconds=120.0, cooldown_seconds=600.0)
        self._llm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="vyxen-llm")
        self._action_worker: Optional[asyncio.Task] = None
        self._last_actions: dict[str, dict] = {}
        self._action_journal = ActionJournal()
        self._macros = MacroStore()
        self._scheduler = ScheduleStore()
        self._setup_wizards = SetupWizardStore()
        self._faqs = FaqStore()
        self._activity_counts: dict[str, dict] = {}
        self._user_activity: dict[str, dict] = {}

    async def setup_hook(self) -> None:
        self._action_worker = asyncio.create_task(self._action_executor())

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            try:
                self._llm_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def _prepare_content(self, content: str) -> tuple[str, bool]:
        """
        Apply an absolute safety filter to outgoing text. If unsafe content is detected,
        return a neutral replacement and indicate it was blocked.
        """
        if not content:
            return content, False
        lowered = content.lower()
        unsafe_patterns = [
            r"\bnazi\b",
            r"\bkkk\b",
            r"\bhitler\b",
            r"\bnig{1,}[\w]*",  # racial slur root
            r"\bfag{1,}[\w]*",  # homophobic slur root
            r"\bcoon\b",
            r"\bslut\b",
            r"\bwhore\b",
            r"\bporn\b",
            r"\bsexual\b",
            r"\bnsfw\b",
            r"\bhate\s+speech\b",
            r"\bharass",
            r"\bkill\s+yourself\b",
        ]
        import re

        for pat in unsafe_patterns:
            if re.search(pat, lowered):
                safe = "I can’t share that. Let’s keep things respectful."
                try:
                    print("[SAFETY] Blocked unsafe outgoing content", flush=True)
                except Exception:
                    pass
                return safe, True
        return content, False

    def _record_last_action(self, author_id: str, action_type: str, detail: str, reversible: bool = False) -> None:
        try:
            self._last_actions[str(author_id)] = {
                "type": action_type,
                "detail": detail,
                "reversible": reversible,
                "timestamp": time.time(),
            }
        except Exception:
            pass

    def _record_action_journal(
        self,
        author_id: str,
        action_type: str,
        targets: dict,
        before_state: dict | None,
        after_state: dict | None,
        reversible: bool,
    ) -> None:
        try:
            self._action_journal.record(
                user_id=str(author_id),
                action_type=action_type,
                targets=targets,
                before_state=before_state,
                after_state=after_state,
                reversible=reversible,
            )
            self._record_last_action(author_id, action_type, str(targets), reversible)
        except Exception:
            pass

    def _serialize_overwrites(self, overwrites: discord.PermissionOverwrite) -> dict:
        data: dict[str, object] = {}
        try:
            for key, value in overwrites:
                data[key] = value
        except Exception:
            try:
                for key in getattr(discord.Permissions, "VALID_FLAGS", []):
                    data[key] = getattr(overwrites, key, None)
            except Exception:
                pass
        return data

    async def _undo_action(self, entry: ActionEntry, guild: discord.Guild, context_channel: discord.abc.GuildChannel, author_id: str) -> ActionResult:
        try:
            atype = entry.action_type
            targets = entry.targets or {}
            before = entry.before_state or {}
            after = entry.after_state or {}
            if atype in {"create_role", "create_role_from_setup"}:
                role_id = after.get("role_id")
                if role_id:
                    role_obj = guild.get_role(int(role_id))
                    if role_obj:
                        await role_obj.delete(reason=f"Vyxen undo by {author_id}")
                        return ActionResult(intent=None, success=True, detail="Undid create_role by deleting role")
                return ActionResult(intent=None, success=False, detail="Role missing for undo")
            if atype == "create_category":
                category_id = after.get("category_id")
                if category_id:
                    cat = guild.get_channel(int(category_id))
                    if cat:
                        await cat.delete(reason=f"Vyxen undo create category by {author_id}")
                        return ActionResult(intent=None, success=True, detail="Deleted created category")
                return ActionResult(intent=None, success=False, detail="Category missing for undo")
            if atype == "delete_role":
                name = before.get("name")
                perms_val = before.get("permissions")
                if not name:
                    return ActionResult(intent=None, success=False, detail="Missing role data for undo")
                perms = discord.Permissions(perms_val) if perms_val is not None else discord.Permissions.none()
                new_role = await guild.create_role(name=name, permissions=perms, reason=f"Vyxen undo delete role by {author_id}")
                return ActionResult(intent=None, success=True, detail=f"Recreated role {new_role.name}")
            if atype in {"create_text_channel", "create_voice_channel", "create_channel"}:
                channel_id = after.get("channel_id")
                if channel_id:
                    ch = guild.get_channel(int(channel_id))
                    if ch:
                        await ch.delete(reason=f"Vyxen undo by {author_id}")
                        return ActionResult(intent=None, success=True, detail="Deleted created channel")
                return ActionResult(intent=None, success=False, detail="Channel missing for undo")
            if atype == "delete_channel":
                name = before.get("name")
                ctype = before.get("type")
                category_id = before.get("category_id")
                if not name or not ctype:
                    return ActionResult(intent=None, success=False, detail="Missing channel data for undo")
                category = guild.get_channel(category_id) if category_id else None
                if ctype == "voice":
                    new_ch = await guild.create_voice_channel(name=name, category=category, reason=f"Vyxen undo delete channel by {author_id}")
                else:
                    new_ch = await guild.create_text_channel(name=name, category=category, reason=f"Vyxen undo delete channel by {author_id}")
                return ActionResult(intent=None, success=True, detail=f"Recreated channel {getattr(new_ch, 'name', '')}")
            if atype == "move_channel_to_category":
                channel_id = targets.get("channel_id")
                prev_category_id = before.get("from_category_id")
                if channel_id is None:
                    return ActionResult(intent=None, success=False, detail="Missing channel for undo")
                ch = guild.get_channel(int(channel_id))
                if ch is None:
                    return ActionResult(intent=None, success=False, detail="Channel not found for undo")
                category_obj = None
                if prev_category_id:
                    category_obj = guild.get_channel(int(prev_category_id))
                await ch.edit(category=category_obj, reason=f"Vyxen undo move channel by {author_id}")
                return ActionResult(intent=None, success=True, detail="Moved channel back")
            if atype == "permission_check_and_fix":
                channel_id = targets.get("channel_id")
                role_id = targets.get("role_id")
                before_over = before.get("overwrites")
                if channel_id is None or role_id is None or before_over is None:
                    return ActionResult(intent=None, success=False, detail="Missing data for undo")
                ch = guild.get_channel(int(channel_id)) or context_channel
                role = guild.get_role(int(role_id))
                if ch is None or role is None:
                    return ActionResult(intent=None, success=False, detail="Channel or role missing for undo")
                ow = ch.overwrites_for(role)
                for k, v in before_over.items():
                    if hasattr(ow, k):
                        setattr(ow, k, v)
                await ch.set_permissions(role, overwrite=ow, reason=f"Vyxen undo permissions by {author_id}")
                return ActionResult(intent=None, success=True, detail="Restored previous overwrites")
            if atype == "lock_category":
                category_id = targets.get("category_id")
                before_map = before.get("overwrites")
                if category_id is None or before_map is None:
                    return ActionResult(intent=None, success=False, detail="Missing data for undo")
                category_obj = guild.get_channel(int(category_id))
                if category_obj is None:
                    return ActionResult(intent=None, success=False, detail="Category missing for undo")
                # Restore overwrites for recorded targets on category and children.
                for ch_id, target_map in before_map.items():
                    ch = guild.get_channel(int(ch_id))
                    if ch is None:
                        continue
                    for target_id, overw in target_map.items():
                        target_role = guild.get_role(int(target_id))
                        if target_role is None:
                            continue
                        ow = ch.overwrites_for(target_role)
                        for k, v in overw.items():
                            if hasattr(ow, k):
                                setattr(ow, k, v)
                        try:
                            await ch.set_permissions(target_role, overwrite=ow, reason=f"Vyxen undo lock by {author_id}")
                        except Exception:
                            continue
                return ActionResult(intent=None, success=True, detail="Restored category visibility")
            if atype == "role_permissions_update":
                role_id = targets.get("role_id")
                perms_val = before.get("permissions")
                if role_id is None or perms_val is None:
                    return ActionResult(intent=None, success=False, detail="Missing role data for undo")
                role = guild.get_role(int(role_id))
                if role is None:
                    return ActionResult(intent=None, success=False, detail="Role missing for undo")
                perms = discord.Permissions(perms_val)
                await role.edit(permissions=perms, reason=f"Vyxen undo role perms by {author_id}")
                return ActionResult(intent=None, success=True, detail="Restored role permissions")
            return ActionResult(intent=None, success=False, detail="Undo type not supported")
        except Exception as exc:
            return ActionResult(intent=None, success=False, detail=f"Undo failed: {exc}")

    async def on_ready(self) -> None:
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        guild_count = len(self.guilds)
        channel_count = sum(len(g.channels) for g in self.guilds)
        print(
            f"[READY] Vyxen online as {self.user} | guilds={guild_count} channels={channel_count} mem={mem_mb:.1f} MB",
            flush=True,
        )
        # Snapshot emission disabled in Safe Mode to avoid heavy memory churn

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        try:
            if message.guild:
                gkey = str(message.guild.id)
                ckey = message.channel.id
                self._activity_counts.setdefault(gkey, {}).setdefault(ckey, {"total": 0, "per_day": {}})
                entry = self._activity_counts[gkey][ckey]
                entry["total"] += 1
                day = time.strftime("%Y-%m-%d", time.gmtime())
                entry["per_day"][day] = entry["per_day"].get(day, 0) + 1
                ukey = str(message.author.id)
                record = self._user_activity.setdefault(gkey, {}).setdefault(ukey, {"total": 0})
                record["total"] = record.get("total", 0) + 1
                record["last_ts"] = message.created_at.timestamp()
                record.setdefault("first_seen", message.created_at.timestamp())
        except Exception:
            pass
        salience = self._calculate_salience(message)
        mentioned_user_ids = [
            m.id for m in message.mentions if not m.bot and (self.user is None or m.id != self.user.id)
        ]
        channel_mentions = [c.id for c in message.channel_mentions]
        role_mentions = [r.id for r in message.role_mentions]
        perms = message.channel.permissions_for(message.author)
        author_id_str = str(message.author.id)
        whitelisted_admin = author_id_str in self.config.admin_user_ids
        wizard_session = None
        wizard_stage = None
        if message.guild:
            wizard_session = self._setup_wizards.active(str(message.guild.id), str(message.author.id))
            wizard_stage = wizard_session.stage if wizard_session else None
        lower_content = message.content.lower()
        content_for_stimulus = message.content

        # Admin-only safe mode toggles handled inline to avoid cognition delays
        adminish = (
            perms.administrator
            or perms.manage_channels
            or perms.manage_roles
            or whitelisted_admin
        )
        if "safe mode" in lower_content:
            off_phrases = [
                "exit safe mode",
                "turn off safe mode",
                "disable safe mode",
                "safe mode off",
            ]
            on_phrases = [
                "enter safe mode",
                "enable safe mode",
                "turn on safe mode",
                "safe mode on",
            ]
            if any(phrase in lower_content for phrase in off_phrases):
                if not adminish:
                    await message.channel.send("I can only change Safe Mode for admins.")
                    return
                if self.cognition.state.safe_mode:
                    self.cognition.state.safe_mode = False
                    self.cognition.memory.allow_writes = True
                    await message.channel.send("Safe Mode exited. I’ll stay cautious but can act within limits.")
                else:
                    await message.channel.send("I’m already out of Safe Mode.")
                # Allow additional commands in the same message (after stripping the toggle phrase).
                try:
                    for phrase in off_phrases:
                        content_for_stimulus = re.sub(re.escape(phrase), "", content_for_stimulus, flags=re.IGNORECASE)
                except Exception:
                    pass
                if not content_for_stimulus.strip():
                    return
                lower_content = content_for_stimulus.lower()
            elif any(phrase in lower_content for phrase in on_phrases):
                if not adminish:
                    await message.channel.send("Safe Mode changes are admin-only.")
                    return
                self.cognition.state.safe_mode = True
                self.cognition.memory.allow_writes = False
                await message.channel.send("Safe Mode enabled. I’ll keep everything read-only.")
                try:
                    for phrase in on_phrases:
                        content_for_stimulus = re.sub(re.escape(phrase), "", content_for_stimulus, flags=re.IGNORECASE)
                except Exception:
                    pass
                if not content_for_stimulus.strip():
                    return
                lower_content = content_for_stimulus.lower()

        stimulus = Stimulus(
            type="discord_message",
            source="discord",
            context={
                "server_id": str(message.guild.id) if message.guild else "dm",
                "channel_id": message.channel.id,
                "author_id": message.author.id,
                "message_id": message.id,
                "content": content_for_stimulus,
                "attachments": [a.id for a in message.attachments],
                "mentions_bot": self.user in message.mentions if self.user else False,
                "mentioned_user_ids": mentioned_user_ids,
                "channel_mentions": channel_mentions,
                "role_mentions": role_mentions,
                "author_whitelisted": whitelisted_admin,
                "setup_wizard_active": wizard_session is not None,
                "setup_wizard_stage": wizard_stage,
                "author_permissions": {
                    "administrator": perms.administrator,
                    "manage_channels": perms.manage_channels,
                    "manage_roles": perms.manage_roles,
                    "manage_permissions": perms.manage_roles or perms.manage_channels,
                },
            },
            salience=salience,
            timestamp=message.created_at.timestamp(),
        )
        await self._enqueue_stimulus(stimulus)
        if stimulus.context.get("mentions_bot"):
            try:
                print(
                    f"[MENTION] from {message.author} in #{message.channel} msg_id={message.id} content_len={len(message.content)}"
                )
            except Exception:
                pass

        for attachment in message.attachments:
            attachment_stimulus = Stimulus(
                type="attachment",
                source="discord",
                context={
                    "server_id": str(message.guild.id) if message.guild else "dm",
                    "channel_id": message.channel.id,
                    "author_id": message.author.id,
                    "attachment_id": attachment.id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "mentions_bot": self.user in message.mentions if self.user else False,
                    "mentioned_user_ids": mentioned_user_ids,
                },
                salience=min(1.0, salience + 0.1),
                timestamp=message.created_at.timestamp(),
            )
            await self._enqueue_stimulus(attachment_stimulus)

    async def on_member_join(self, member: discord.Member) -> None:
        stim = Stimulus(
            type="discord_member_join",
            source="discord",
            context={
                "server_id": str(member.guild.id),
                "member_id": member.id,
                "member_name": member.display_name,
            },
            salience=0.4,
        )
        await self._enqueue_stimulus(stim)

    async def on_member_remove(self, member: discord.Member) -> None:
        stim = Stimulus(
            type="discord_member_leave",
            source="discord",
            context={
                "server_id": str(member.guild.id),
                "member_id": member.id,
                "member_name": member.display_name,
            },
            salience=0.3,
        )
        await self._enqueue_stimulus(stim)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        return

    async def on_guild_role_create(self, role: discord.Role) -> None:
        return

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        return

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        return

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        return

    async def _action_executor(self) -> None:
        while True:
            try:
                intent: ActionIntent = await self.action_queue.get()
            except asyncio.CancelledError:
                return
            try:
                result = await self._execute_intent(intent)
            except Exception as exc:
                print(f"[ACTION-ERROR] {exc}")
                result = ActionResult(intent=intent, success=False, detail=str(exc))
            else:
                if intent.type != "observe":
                    print(
                        f"[ACTION] {intent.type} target={intent.target_id} success={getattr(result,'success',None)} detail={getattr(result,'detail','')}"
                    )

            audit_context = intent.metadata.get("audit_context")
            if audit_context:
                try:
                    log_decision(
                        self.logger,
                        stimulus=audit_context["stimulus"],
                        realities=audit_context["realities"],
                        governor_choice=audit_context["governor_choice"],
                        action_result=result.to_dict(include_metadata=False),
                    )
                except Exception as exc:
                    print(f"[AUDIT-ERROR] log_decision failed: {exc}")
            if not result.success:
                print(f"[ACTION-FAIL] type={intent.type} target={intent.target_id} detail={result.detail}")

    async def _execute_intent(self, intent: ActionIntent) -> ActionResult:
        key = f"{intent.type}:{intent.target_id}"
        if not self.rate_limiter.allow(key):
            return ActionResult(intent=intent, success=False, detail="Rate limit exceeded")

        try:
            if self.cognition.state.safe_mode and intent.type not in {"observe", "reply", "send_message"}:
                return ActionResult(intent=intent, success=False, detail="Safe Mode blocks execution")
            if intent.type == "observe":
                return ActionResult(intent=intent, success=True, detail="Observation only")
            if intent.type == "send_message":
                res = await self._send_message(intent)
                if not res.success:
                    print(f"[ACTION] send_message failed: {res.detail}")
                return res
            if intent.type == "reply":
                res = await self._reply(intent)
                if not res.success:
                    print(f"[ACTION] reply failed: {res.detail}")
                return res
            if intent.type == "react":
                return await self._react(intent)
            if intent.type == "defer":
                return await self._defer(intent)
            if intent.type == "schedule":
                return await self._schedule(intent)
            if intent.type == "tool_call":
                return await self._tool_call(intent)
        except Exception as exc:
            if intent.type == "tool_call":
                self.tool_breaker.record_failure(str(exc))
            return ActionResult(intent=intent, success=False, detail=str(exc))

        return ActionResult(intent=intent, success=False, detail="Unknown action type")

    async def _send_message(self, intent: ActionIntent) -> ActionResult:
        try:
            channel = await self._get_channel(intent.target_id)
            if not channel:
                return ActionResult(intent=intent, success=False, detail="Channel not found")
            if not self._can_send(channel):
                return ActionResult(intent=intent, success=False, detail="Missing permission")
            content_raw = intent.payload.get("content", "")
            content, blocked = self._prepare_content(content_raw)
            await channel.send(content)
            return ActionResult(intent=intent, success=True, detail="Message sent" + (" (filtered)" if blocked else ""))
        except Exception as exc:
            print(f"[ACTION-ERROR] send_message: {exc}")
            return ActionResult(intent=intent, success=False, detail=str(exc))

    async def _reply(self, intent: ActionIntent) -> ActionResult:
        try:
            channel = await self._get_channel(intent.target_id)
            if not channel:
                return ActionResult(intent=intent, success=False, detail="Channel not found")
            if not self._can_send(channel):
                return ActionResult(intent=intent, success=False, detail="Missing permission")
            reply_to = intent.payload.get("reply_to")
            content_raw = intent.payload.get("content", "")
            content = content_raw
            if (not content) and intent.metadata.get("llm") and not self.cognition.state.safe_mode:
                try:
                    content = await self._compose_llm_reply(intent)
                except Exception as exc:
                    print(f"[LLM-ERROR] reply composition failed: {exc}", flush=True)
                    content = "I’m here—give me a sec and try that again?"
            if not content:
                content = "I’m here."
            content, blocked = self._prepare_content(content)
            if reply_to:
                try:
                    # Avoid hanging on slow fetch; fall back to plain send on timeout/failure
                    message = await asyncio.wait_for(channel.fetch_message(reply_to), timeout=3.0)
                    await message.reply(content)
                except Exception as exc:
                    # Fallback if fetch_message fails
                    print(f"[ACTION-ERROR] reply fetch failed; falling back to send: {exc}")
                    await channel.send(content)
            else:
                await channel.send(content)
            return ActionResult(intent=intent, success=True, detail="Reply sent" + (" (filtered)" if blocked else ""))
        except Exception as exc:
            print(f"[ACTION-ERROR] reply: {exc}")
            return ActionResult(intent=intent, success=False, detail=str(exc))

    async def _compose_llm_reply(self, intent: ActionIntent) -> str:
        """
        Compose a conversational reply via the LLM, outside the cognition tick budget.
        """
        user_content = intent.payload.get("user_content") or ""
        if not user_content:
            return ""
        server_id = intent.metadata.get("server_id") or "global"
        author_id = intent.metadata.get("author_id")
        try:
            from vyxen_core.memory import extract_topics
            from vyxen_core.llm import craft_social_reply
        except Exception:
            return ""

        profile = {}
        important = {}
        if author_id is not None:
            try:
                profile = self.cognition.memory.get_user_profile(server_id, str(author_id))
            except Exception:
                profile = {}
            try:
                important = self.cognition.memory.get_important(server_id, str(author_id))
            except Exception:
                important = {}

        shared_topics: list[str] = []
        try:
            topics = extract_topics(user_content)
            shared_entries = self.cognition.memory.fetch_shared_context(server_id, topics)
            shared_topics = [topic for topic, _, _ in shared_entries]
        except Exception:
            shared_topics = []

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._llm_executor,
            craft_social_reply,
            user_content,
            self.cognition.identity.values,
            profile or {},
            shared_topics,
            important or {},
        )
        done, pending = await asyncio.wait({future}, timeout=20.0)
        if pending:
            try:
                future.cancel()
            except Exception:
                pass
            return "Sorry—give me a second, I’m thinking."
        try:
            reply = next(iter(done)).result()
        except Exception as exc:
            print(f"[LLM-ERROR] reply composition failed: {exc}", flush=True)
            return "I’m here—give me a sec and try that again?"
        return (reply or "").strip()[:1800]

    async def _react(self, intent: ActionIntent) -> ActionResult:
        try:
            channel = await self._get_channel(intent.target_id)
            if not channel:
                return ActionResult(intent=intent, success=False, detail="Channel not found")
            message_id = intent.payload.get("message_id")
            emoji = intent.payload.get("emoji", "👀")
            if not message_id:
                return ActionResult(intent=intent, success=False, detail="Missing message_id")
            message = await channel.fetch_message(message_id)
            await message.add_reaction(emoji)
            return ActionResult(intent=intent, success=True, detail="Reaction added")
        except Exception as exc:
            print(f"[ACTION-ERROR] react: {exc}")
            return ActionResult(intent=intent, success=False, detail=str(exc))

    async def _defer(self, intent: ActionIntent) -> ActionResult:
        try:
            channel = await self._get_channel(intent.target_id)
            if not channel:
                return ActionResult(intent=intent, success=False, detail="Channel not found")
            message_id = intent.payload.get("message_id")
            if not message_id:
                return ActionResult(intent=intent, success=False, detail="Missing message_id")
            message = await channel.fetch_message(message_id)
            await message.add_reaction("⏳")
            return ActionResult(intent=intent, success=True, detail="Deferred with reaction")
        except Exception as exc:
            print(f"[ACTION-ERROR] defer: {exc}")
            return ActionResult(intent=intent, success=False, detail=str(exc))

    async def _schedule(self, intent: ActionIntent) -> ActionResult:
        if self.cognition.state.safe_mode:
            return ActionResult(intent=intent, success=False, detail="Safe Mode blocks scheduling")
        delay = intent.payload.get("delay", 5)
        asyncio.create_task(self._run_scheduled(intent, delay))
        return ActionResult(intent=intent, success=True, detail="Scheduled follow-up")

    async def _run_scheduled(self, intent: ActionIntent, delay: float) -> None:
        if self.cognition.state.safe_mode:
            return
        await asyncio.sleep(delay)
        follow_intent = ActionIntent(
            type="reply",
            target_id=intent.target_id,
            payload={
                "reply_to": intent.payload.get("reply_to"),
                "content": intent.payload.get("content", ""),
            },
            metadata=intent.metadata,
        )
        await self.action_queue.put(follow_intent)

    async def _emit_server_snapshot(self, guild: discord.Guild) -> None:
        roles = [
            {"id": role.id, "name": role.name, "position": role.position}
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        ]
        channels = [
            {"id": ch.id, "name": getattr(ch, 'name', ''), "type": str(ch.type)}
            for ch in guild.channels
        ]
        stim = Stimulus(
            type="server_snapshot",
            source="discord",
            context={
                "server_id": str(guild.id),
                "roles": roles[:40],
                "channels": channels[:60],
                "member_count": guild.member_count,
            },
            salience=0.2,
        )
        await self._enqueue_stimulus(stim)

    async def _emit_server_event(self, guild: discord.Guild, event_type: str, data: dict) -> None:
        stim = Stimulus(
            type="server_event",
            source="discord",
            context={
                "server_id": str(guild.id),
                "event_type": event_type,
                "data": data,
            },
            salience=0.2,
        )
        await self._enqueue_stimulus(stim)

    async def _tool_call(self, intent: ActionIntent) -> ActionResult:
        if self.cognition.state.safe_mode:
            return ActionResult(intent=intent, success=False, detail="Safe Mode: tool execution disabled")
        if not self.tool_breaker.allow():
            return ActionResult(intent=intent, success=False, detail="Tool execution circuit open")
        guild_id = intent.metadata.get("guild_id")
        author_id = intent.metadata.get("author_id")
        intent_type = intent.payload.get("intent_type")
        context_channel_id = intent.target_id or intent.payload.get("target_channel")
        channel_id = intent.payload.get("target_channel") or context_channel_id
        role_id = intent.payload.get("target_role")
        requested_changes = intent.payload.get("requested_changes", {}) or {}

        if guild_id is None or author_id is None or context_channel_id is None:
            return ActionResult(intent=intent, success=False, detail="Missing context for tool call")

        guild = self.get_guild(int(guild_id)) if guild_id != "dm" else None
        if guild is None:
            return ActionResult(intent=intent, success=False, detail="Guild not accessible")
        context_channel = guild.get_channel(int(context_channel_id))
        if context_channel is None:
            try:
                context_channel = await self.fetch_channel(int(context_channel_id))
            except Exception as exc:
                return ActionResult(intent=intent, success=False, detail=f"Context channel not found: {exc}")

        # Channel targeted for permissions checks/overwrites; defaults to the current channel.
        channel = guild.get_channel(int(channel_id)) if channel_id is not None else context_channel
        if channel is None and channel_id is not None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except Exception as exc:
                return ActionResult(intent=intent, success=False, detail=f"Channel not found: {exc}")
        if channel is None:
            channel = context_channel

        member = guild.get_member(int(author_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(author_id))
            except Exception:
                return ActionResult(intent=intent, success=False, detail="Member not found")
        perms = context_channel.permissions_for(member)
        author_whitelisted_admin = str(author_id) in self.config.admin_user_ids
        has_admin = (
            perms.administrator
            or perms.manage_roles
            or perms.manage_channels
            or author_whitelisted_admin
        )
        read_only_public = {
            "list_roles",
            "list_channels",
            "server_stats_report",
            "last_action_explain",
            "server_activity_report",
            "answer_faq",
            "list_faqs",
        }
        if not has_admin and intent_type not in read_only_public:
            await context_channel.send("I can’t do admin changes without admin or manage-permissions rights.")
            return ActionResult(intent=intent, success=False, detail="Insufficient permissions")

        is_dry_run = bool(intent.metadata.get("dry_run") or intent.metadata.get("dry_run_message"))
        if is_dry_run and intent_type not in {"list_roles", "list_channels", "server_stats_report", "role_permissions_report", "user_profile_report", "last_action_explain"}:
            try:
                summary = intent.metadata.get("request_summary") or requested_changes
                msg = f"Dry run: I won’t change anything. I would execute `{intent_type}` with: {summary}"
                msg, _ = self._prepare_content(msg)
                await context_channel.send(msg)
            except Exception:
                pass
            return ActionResult(intent=intent, success=False, detail="Dry-run only; no changes executed")

        async def _send_progress(msg: str) -> None:
            try:
                safe_msg, _ = self._prepare_content(msg)
                await context_channel.send(safe_msg)
            except Exception:
                return

        if intent_type == "permission_check_and_fix":
            if role_id is None:
                return ActionResult(intent=intent, success=False, detail="No role specified")
            role = guild.get_role(int(role_id))
            if role is None:
                return ActionResult(intent=intent, success=False, detail="Role not found")

            before_overwrite_state = self._serialize_overwrites(channel.overwrites_for(role))

            perm_spec = requested_changes.get("permissions")
            if not isinstance(perm_spec, dict):
                perm_spec = {}
            if not perm_spec:
                # Back-compat: older intents put the flag at the top level.
                try:
                    for key, value in (requested_changes or {}).items():
                        if isinstance(key, str) and hasattr(discord.Permissions, "VALID_FLAGS") and key in discord.Permissions.VALID_FLAGS:
                            perm_spec[key] = value
                except Exception:
                    pass
            if not perm_spec:
                perm_spec = {"view_channel": True}

            overwrites = channel.overwrites_for(role)
            changes: list[tuple[str, object, object]] = []
            allow_list: list[str] = []
            deny_list: list[str] = []
            clear_list: list[str] = []
            for perm_name, perm_value in perm_spec.items():
                if not isinstance(perm_name, str) or not hasattr(overwrites, perm_name):
                    continue
                before_val = getattr(overwrites, perm_name, None)
                if perm_value is None:
                    setattr(overwrites, perm_name, None)
                    clear_list.append(perm_name)
                elif isinstance(perm_value, bool):
                    setattr(overwrites, perm_name, perm_value)
                    (allow_list if perm_value else deny_list).append(perm_name)
                else:
                    coerced = bool(perm_value)
                    setattr(overwrites, perm_name, coerced)
                    (allow_list if coerced else deny_list).append(perm_name)
                after_val = getattr(overwrites, perm_name, None)
                if before_val != after_val:
                    changes.append((perm_name, before_val, after_val))

            try:
                if not changes:
                    await _send_progress(f"Permissions for {role.name} in {channel.mention} already matched what you asked for.")
                    self.tool_breaker.record_success()
                    return ActionResult(intent=intent, success=True, detail="No changes needed")

                await channel.set_permissions(role, overwrite=overwrites, reason="Vyxen tool call update")

                def _clip(items: list[str], limit: int = 8) -> str:
                    if len(items) <= limit:
                        return ", ".join(items)
                    return ", ".join(items[:limit]) + f", +{len(items) - limit}"

                parts: list[str] = []
                if allow_list:
                    parts.append(f"allow: {_clip(allow_list)}")
                if deny_list:
                    parts.append(f"deny: {_clip(deny_list)}")
                if clear_list:
                    parts.append(f"clear: {_clip(clear_list)}")
                summary = "; ".join(parts) if parts else "updated overwrites"

                detail = "; ".join([f"{name}:{before}->{after}" for name, before, after in changes[:8]])
                await _send_progress(f"Updated permissions for {role.name} in {channel.mention} ({summary}).")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"channel_id": channel.id, "role_id": role.id},
                        {"overwrites": before_overwrite_state},
                        {"overwrites": self._serialize_overwrites(overwrites)},
                        reversible=True,
                    )
                return ActionResult(intent=intent, success=True, detail=detail)
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                return ActionResult(intent=intent, success=False, detail=f"Failed to set permissions: {exc}")
        elif intent_type == "permission_check":
            if role_id is None:
                return ActionResult(intent=intent, success=False, detail="No role specified")
            role = guild.get_role(int(role_id))
            if role is None:
                return ActionResult(intent=intent, success=False, detail="Role not found")
            overwrites = channel.overwrites_for(role)
            await _send_progress(
                f"Permissions for {role.name} here: view_channel={overwrites.view_channel}, send_messages={overwrites.send_messages}"
            )
            self.tool_breaker.record_success()
            return ActionResult(intent=intent, success=True, detail="Reported permissions")

        elif intent_type == "role_permissions_report":
            role_obj = None
            if role_id is not None:
                try:
                    role_obj = guild.get_role(int(role_id))
                except Exception:
                    role_obj = None
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    try:
                        role_obj = discord.utils.get(guild.roles, name=role_name)
                    except Exception:
                        role_obj = None
                    if role_obj is None:
                        for r in guild.roles:
                            if r.name.lower() == role_name.lower():
                                role_obj = r
                                break
            if role_obj is None:
                await _send_progress("Which role should I audit? Mention it like @Admin (or quote the role name).")
                return ActionResult(intent=intent, success=False, detail="Role not found")

            try:
                allowed = [name for name, value in role_obj.permissions if value]
                allowed.sort()
                shown = allowed[:40]
                extra = len(allowed) - len(shown)
                lines = [f"Role `{role_obj.name}` permissions ({len(allowed)} enabled):"]
                if shown:
                    lines.append(", ".join(shown) + (f" (+{extra})" if extra > 0 else ""))
                else:
                    lines.append("(none)")
                await _send_progress("\n".join(lines))
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail=f"Reported permissions for {role_obj.name}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                return ActionResult(intent=intent, success=False, detail=f"Role permissions report failed: {exc}")

        elif intent_type == "role_permissions_update":
            role_obj = None
            if role_id is not None:
                try:
                    role_obj = guild.get_role(int(role_id))
                except Exception:
                    role_obj = None
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    try:
                        role_obj = discord.utils.get(guild.roles, name=role_name)
                    except Exception:
                        role_obj = None
                    if role_obj is None:
                        for r in guild.roles:
                            if r.name.lower() == role_name.lower():
                                role_obj = r
                                break
            if role_obj is None:
                await _send_progress("Which role should I change? Mention it like @Admin (or quote the role name).")
                return ActionResult(intent=intent, success=False, detail="Role not found")
            if role_obj == guild.default_role:
                return ActionResult(intent=intent, success=False, detail="Refusing to change @everyone role permissions")
            if getattr(role_obj, "managed", False):
                return ActionResult(intent=intent, success=False, detail="Refusing to change a managed/integration role")

            perm_spec = requested_changes.get("permissions")
            if not isinstance(perm_spec, dict) or not perm_spec:
                return ActionResult(intent=intent, success=False, detail="No permissions specified")

            # Role permissions are booleans; treat None (unset) as False for updates.
            cleaned: dict[str, bool] = {}
            for perm_name, perm_value in perm_spec.items():
                if not isinstance(perm_name, str):
                    continue
                if perm_value is None:
                    cleaned[perm_name] = False
                elif isinstance(perm_value, bool):
                    cleaned[perm_name] = perm_value
                else:
                    cleaned[perm_name] = bool(perm_value)

            before = role_obj.permissions
            after = discord.Permissions(before.value)
            changes: list[str] = []
            for perm_name, perm_value in cleaned.items():
                if not hasattr(after, perm_name):
                    continue
                before_val = getattr(before, perm_name, None)
                setattr(after, perm_name, perm_value)
                after_val = getattr(after, perm_name, None)
                if before_val != after_val:
                    changes.append(f"{perm_name}:{before_val}->{after_val}")

            if not changes:
                await _send_progress(f"Role `{role_obj.name}` already has those permissions set.")
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="No changes needed")

            try:
                await role_obj.edit(permissions=after, reason=f"Vyxen role permission update by {author_id}")
                shown = ", ".join(changes[:10]) + (f", +{len(changes)-10}" if len(changes) > 10 else "")
                await _send_progress(f"Updated `{role_obj.name}` permissions. ({shown})")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"role_id": role_obj.id},
                        {"permissions": before.value},
                        {"permissions": after.value},
                        reversible=True,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Updated role perms {role_obj.name}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t update that role’s permissions: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Role permission update failed: {exc}")

        elif intent_type == "list_roles":
            try:
                roles = [r for r in guild.roles if r != guild.default_role]
                roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
                names = [f"`{r.name}`" for r in roles_sorted]
                shown = names[:60]
                extra = len(names) - len(shown)
                msg = f"Roles in this server ({len(names)}):\n" + ", ".join(shown)
                if extra > 0:
                    msg += f"\n(+{extra} more)"
                await _send_progress(msg[:1800])
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="Listed roles")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t list roles: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"List roles failed: {exc}")

        elif intent_type == "server_stats_report":
            try:
                text_channels = list(getattr(guild, "text_channels", []) or [])
                voice_channels = list(getattr(guild, "voice_channels", []) or [])
                categories = list(getattr(guild, "categories", []) or [])
                role_count = max(0, len(getattr(guild, "roles", []) or []) - 1)
                member_count = getattr(guild, "member_count", None)
                lines = [
                    f"Server: `{guild.name}`",
                ]
                if member_count is None:
                    lines.append("Members: I don’t have that information yet.")
                else:
                    lines.append(f"Members: {member_count}")
                lines.append(f"Roles: {role_count}")
                lines.append(f"Channels: {len(text_channels)} text, {len(voice_channels)} voice, {len(categories)} categories")
                try:
                    boosts = getattr(guild, "premium_subscription_count", None)
                    tier = getattr(guild, "premium_tier", None)
                    if boosts is not None:
                        lines.append(f"Boosts: {boosts} (tier {tier})")
                except Exception:
                    pass
                await _send_progress("\n".join(lines)[:1800])
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="Reported server stats")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t pull server stats: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Server stats failed: {exc}")

        elif intent_type == "server_activity_report":
            # Report strictly from journal/logged actions and cached counts—no LLM.
            entries: list[ActionEntry] = []
            try:
                bucket = getattr(self._action_journal, "_entries", {}).values()
                for b in bucket:
                    entries.extend(b)
            except Exception:
                entries = []
            if not entries:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No activity data")
            recent = sorted(entries, key=lambda e: e.timestamp, reverse=True)[:8]
            lines = []
            for entry in recent:
                when = int(max(0, time.time() - entry.timestamp) // 60)
                age = f"{when}m ago" if when else "just now"
                targets = entry.targets
                target_desc = ", ".join(f"{k}={v}" for k, v in targets.items() if v) if targets else ""
                lines.append(f"{age}: {entry.action_type} ({target_desc})")
            await _send_progress("\n".join(lines)[:1800])
            return ActionResult(intent=intent, success=True, detail="Reported activity log")

        elif intent_type == "audit_summary":
            entries: list[ActionEntry] = []
            try:
                bucket = getattr(self._action_journal, "_entries", {}).values()
                for b in bucket:
                    entries.extend(b)
            except Exception:
                entries = []
            if not entries:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No audit data")
            # Group by day
            from datetime import datetime
            today = datetime.utcnow().date()
            day_entries = [e for e in entries if datetime.utcfromtimestamp(e.timestamp).date() == today]
            if not day_entries:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No audit data")
            lines = []
            for entry in sorted(day_entries, key=lambda e: e.timestamp, reverse=True)[:12]:
                ts = datetime.utcfromtimestamp(entry.timestamp).strftime("%H:%M")
                target_desc = ", ".join(f"{k}={v}" for k, v in entry.targets.items() if v) if entry.targets else ""
                lines.append(f"{ts} UTC: {entry.action_type} ({target_desc})")
            await _send_progress("Today's admin actions:\n" + "\n".join(lines)[:1800])
            return ActionResult(intent=intent, success=True, detail="Audit summary")

        elif intent_type == "schedule_action":
            action_text = (requested_changes.get("action_text") or "").strip()
            execute_at = requested_changes.get("execute_at")
            confirmed = bool(requested_changes.get("confirmed"))
            if not action_text or not execute_at:
                return ActionResult(intent=intent, success=False, detail="Missing schedule details")
            if not confirmed:
                await _send_progress("This will schedule an admin action for later. If that’s correct, say `confirm schedule` with the same request.")
                return ActionResult(intent=intent, success=True, detail="Confirmation requested")
            task_id = f"{author_id}:{int(time.time())}"

            async def _executor(payload: dict):
                stim = Stimulus(
                    type="discord_message",
                    source="scheduled",
                    context={
                        "server_id": str(guild.id),
                        "channel_id": context_channel.id,
                        "author_id": author_id,
                        "message_id": intent.metadata.get("message_id", 0),
                        "content": payload.get("action_text", ""),
                        "mentions_bot": True,
                        "mentioned_user_ids": [],
                        "channel_mentions": [],
                        "role_mentions": [],
                        "author_whitelisted": author_whitelisted_admin,
                        "author_permissions": {"administrator": True, "manage_permissions": True},
                    },
                    salience=0.8,
                )
                parsed = parse_natural_language_intent(stim)
                if not parsed:
                    await _send_progress("Scheduled task couldn’t be parsed at execution time.")
                    return
                action_intent = ActionIntent(
                    type="tool_call",
                    target_id=context_channel.id,
                    payload={
                        "intent_type": parsed.intent_type,
                        "target_channel": parsed.target_channel,
                        "target_role": parsed.target_role,
                        "requested_changes": parsed.requested_changes,
                    },
                    metadata={
                        "author_id": author_id,
                        "guild_id": str(guild.id),
                        "reason": "scheduled",
                        "dry_run": parsed.dry_run,
                    },
                )
                await self.action_queue.put(action_intent)

            entry = self._scheduler.schedule(task_id, float(execute_at), {"action_text": action_text})
            asyncio.create_task(self._scheduler.run_task(entry, _executor))
            await _send_progress(f"Scheduled to run at {time.ctime(execute_at)}. I’ll confirm after execution.")
            return ActionResult(intent=intent, success=True, detail="Task scheduled")

        elif intent_type == "permission_explain":
            user_id = str(requested_changes.get("user_id") or "").strip()
            if not user_id:
                return ActionResult(intent=intent, success=False, detail="No user specified")
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None
            if member is None:
                await _send_progress("I couldn’t find that user here.")
                return ActionResult(intent=intent, success=False, detail="User not found")
            ch = channel or context_channel
            perms = ch.permissions_for(member)
            reasons: list[str] = []
            can_send = perms.send_messages
            if not can_send:
                reasons.append("Send Messages denied.")
            if not perms.view_channel:
                reasons.append("Cannot view this channel.")
            # Examine overwrites
            ow_summary: list[str] = []
            try:
                overwrites = ch.overwrites
                for target, ow in (overwrites or {}).items():
                    if isinstance(target, discord.Role) and target in member.roles:
                        if ow.send_messages is False:
                            ow_summary.append(f"{target.name} denies send_messages")
                        if ow.view_channel is False:
                            ow_summary.append(f"{target.name} denies view_channel")
                    if target == member:
                        if ow.send_messages is False:
                            ow_summary.append("Member-specific deny send_messages")
                        if ow.view_channel is False:
                            ow_summary.append("Member-specific deny view_channel")
            except Exception:
                pass
            text = f"In {ch.mention}, {member.mention} can_send={can_send}. "
            if reasons:
                text += " ".join(reasons)
            if ow_summary:
                text += " Overwrites: " + "; ".join(ow_summary)
            await _send_progress(text[:1800])
            return ActionResult(intent=intent, success=True, detail="Permission explain")

        elif intent_type == "role_impact_preview":
            member_id = str(requested_changes.get("member_id") or "").strip()
            role_obj = None
            if role_id is not None:
                role_obj = guild.get_role(int(role_id))
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    for r in guild.roles:
                        if r.name.lower() == role_name.lower():
                            role_obj = r
                            break
            if role_obj is None:
                await _send_progress("I couldn’t find that role to preview.")
                return ActionResult(intent=intent, success=False, detail="Role not found")
            member = guild.get_member(int(member_id)) if member_id else None
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except Exception:
                    member = None
            if member is None:
                await _send_progress("I couldn’t find that user.")
                return ActionResult(intent=intent, success=False, detail="User not found")

            current = member.guild_permissions
            added = discord.Permissions((role_obj.permissions.value | current.value) if hasattr(current, "value") else role_obj.permissions.value)
            new_flags = [name for name, value in added if value and not getattr(current, name, False)]
            risky = [p for p in new_flags if p in {"administrator", "manage_guild", "ban_members", "kick_members", "manage_roles"}]
            msg_parts = [f"If {member.mention} gets `{role_obj.name}`:"]
            if new_flags:
                msg_parts.append("New perms: " + ", ".join(new_flags[:15]) + (f" (+{len(new_flags)-15})" if len(new_flags) > 15 else ""))
            if risky:
                msg_parts.append("Risks: " + ", ".join(risky))
            msg = "\n".join(msg_parts)
            await _send_progress(msg[:1800])
            return ActionResult(intent=intent, success=True, detail="Role impact preview")

        elif intent_type == "channel_activity_report":
            gkey = str(guild.id)
            counts = self._activity_counts.get(gkey, {})
            now_day = time.strftime("%Y-%m-%d", time.gmtime())
            if not counts:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No activity data")
            window_days = set()
            for i in range(7):
                from datetime import datetime, timedelta
                day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
                window_days.add(day)
            channel_totals: list[tuple[int, int]] = []
            for cid, meta in counts.items():
                per_day = meta.get("per_day", {})
                total = sum(count for day, count in per_day.items() if day in window_days)
                channel_totals.append((cid, total))
            channel_totals.sort(key=lambda x: x[1], reverse=True)
            top = channel_totals[:5]
            lines = []
            for cid, total in top:
                ch = guild.get_channel(cid)
                name = ch.mention if ch else f"<#{cid}>"
                lines.append(f"{name}: {total} messages (last 7 days)")
            if not lines:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No activity data")
            await _send_progress("\n".join(lines)[:1800])
            return ActionResult(intent=intent, success=True, detail="Channel activity report")

        elif intent_type == "user_activity_summary":
            user_id = str(requested_changes.get("user_id") or "").strip()
            if not user_id:
                return ActionResult(intent=intent, success=False, detail="No user specified")
            gkey = str(guild.id)
            activity = self._user_activity.get(gkey, {}).get(user_id)
            if not activity:
                await _send_progress("I don’t have enough recorded data yet to answer that.")
                return ActionResult(intent=intent, success=True, detail="No activity data")
            member = guild.get_member(int(user_id))
            joined = getattr(member, "joined_at", None)
            msg = f"`{user_id}` has sent {activity.get('total', 0)} message(s)"
            if joined:
                msg += f" since joining on {joined.date()}."
            elif activity.get("first_seen"):
                import datetime

                first = datetime.datetime.utcfromtimestamp(activity["first_seen"]).date()
                msg += f" since first seen on {first}."
            last_ts = activity.get("last_ts")
            if last_ts:
                age_minutes = max(0, int((time.time() - last_ts) // 60))
                if age_minutes >= 1440:
                    days = age_minutes // 1440
                    msg += f" Last message: {days} day(s) ago."
                elif age_minutes >= 60:
                    hours = age_minutes // 60
                    msg += f" Last message: {hours} hour(s) ago."
                else:
                    msg += f" Last message: {age_minutes} minute(s) ago."
            await _send_progress(msg[:1800])
            return ActionResult(intent=intent, success=True, detail="User activity summary")


        elif intent_type == "assign_role":
            member_id = str(requested_changes.get("member_id") or "").strip()
            role_obj = None
            if role_id is not None:
                try:
                    role_obj = guild.get_role(int(role_id))
                except Exception:
                    role_obj = None
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    try:
                        role_obj = discord.utils.get(guild.roles, name=role_name)
                    except Exception:
                        role_obj = None
                    if role_obj is None:
                        for r in guild.roles:
                            if r.name.lower() == role_name.lower():
                                role_obj = r
                                break
            if not member_id:
                return ActionResult(intent=intent, success=False, detail="No member specified")
            if role_obj is None:
                await _send_progress("Which role should I assign? Mention it like @Role (or quote the role name).")
                return ActionResult(intent=intent, success=False, detail="Role not found")
            if role_obj == guild.default_role:
                return ActionResult(intent=intent, success=False, detail="Refusing to assign @everyone role")
            if getattr(role_obj, "managed", False):
                return ActionResult(intent=intent, success=False, detail="Refusing to assign a managed/integration role")

            target = guild.get_member(int(member_id))
            if target is None:
                try:
                    target = await guild.fetch_member(int(member_id))
                except Exception:
                    target = None
            if target is None:
                await _send_progress("I couldn’t find that member in this server.")
                return ActionResult(intent=intent, success=False, detail="Member not found")

            try:
                if role_obj in target.roles:
                    await _send_progress(f"{target.mention} already has `{role_obj.name}`.")
                    self.tool_breaker.record_success()
                    return ActionResult(intent=intent, success=True, detail="No changes needed")
                await target.add_roles(role_obj, reason=f"Vyxen role assign by {author_id}")
                await _send_progress(f"Assigned `{role_obj.name}` to {target.mention}.")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"role_id": role_obj.id, "member_id": member_id},
                        None,
                        {"assigned": True},
                        reversible=False,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Assigned {role_obj.name} -> {member_id}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t assign that role: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Assign role failed: {exc}")

        elif intent_type == "user_profile_report":
            user_id = str(requested_changes.get("user_id") or "").strip()
            if not user_id:
                return ActionResult(intent=intent, success=False, detail="No user id specified")
            server_id = str(guild.id)
            try:
                profile = self.cognition.memory.get_user_profile(server_id, user_id)
            except Exception:
                profile = {}
            try:
                important = self.cognition.memory.get_important(server_id, user_id)
            except Exception:
                important = {}

            important_flat = {k: v.get("value") for k, v in (important or {}).items() if isinstance(v, dict)}
            if not profile and not important_flat:
                await _send_progress(f"I don’t have much remembered about `{user_id}` yet.")
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="No profile data")

            try:
                keys = ["verbosity", "tone_balance", "success_rate", "warmth", "formality", "precision", "brevity_bias"]
                stats = ", ".join(
                    [f"{k}={profile.get(k, 0.5):.2f}" for k in keys if isinstance(profile.get(k, None), (int, float))]
                )
            except Exception:
                stats = ""
            msg = f"User `{user_id}` profile: {stats}".strip()
            if important_flat:
                msg += f"\nImportant notes: {important_flat}"
            await _send_progress(msg[:1800])
            self.tool_breaker.record_success()
            return ActionResult(intent=intent, success=True, detail="Reported user profile")

        elif intent_type == "list_roles":
            try:
                roles = [r for r in guild.roles if r != guild.default_role]
                roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
                names = [f"`{r.name}`" for r in roles_sorted]
                if not names:
                    await _send_progress("I don’t have that information yet.")
                    return ActionResult(intent=intent, success=True, detail="No roles found")
                shown = names[:80]
                extra = len(names) - len(shown)
                msg = f"Roles ({len(names)}): " + ", ".join(shown)
                if extra > 0:
                    msg += f" (+{extra} more)"
                await _send_progress(msg[:1800])
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="Listed roles")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t list roles: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"List roles failed: {exc}")

        elif intent_type == "list_channels":
            try:
                channels = list(getattr(guild, "channels", []) or [])
                if not channels:
                    await _send_progress("I don’t have that information yet.")
                    return ActionResult(intent=intent, success=True, detail="No channels found")
                # Group by type
                text_names = [f"`{ch.name}`" for ch in channels if str(getattr(ch, "type", "")).startswith("text")]
                voice_names = [f"`{ch.name}`" for ch in channels if str(getattr(ch, "type", "")).startswith("voice")]
                category_names = [f"`{ch.name}`" for ch in channels if str(getattr(ch, "type", "")) == "category"]
                parts = []
                if text_names:
                    extra = len(text_names) - 40
                    suffix = f" (+{extra})" if extra > 0 else ""
                    parts.append(f"Text ({len(text_names)}): " + ", ".join(text_names[:40]) + suffix)
                if voice_names:
                    extra = len(voice_names) - 30
                    suffix = f" (+{extra})" if extra > 0 else ""
                    parts.append(f"Voice ({len(voice_names)}): " + ", ".join(voice_names[:30]) + suffix)
                if category_names:
                    extra = len(category_names) - 30
                    suffix = f" (+{extra})" if extra > 0 else ""
                    parts.append(f"Categories ({len(category_names)}): " + ", ".join(category_names[:30]) + suffix)
                msg = "\n".join(parts) if parts else "I don’t have that information yet."
                await _send_progress(msg[:1800])
                self.tool_breaker.record_success()
                return ActionResult(intent=intent, success=True, detail="Listed channels")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t list channels: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"List channels failed: {exc}")

        elif intent_type == "setup_wizard_start":
            session = self._setup_wizards.start(str(guild.id), str(author_id))
            prompt = self._setup_wizards.next_prompt(session) or "Tell me the main purpose of this server."
            intro = "Setup wizard started. I’ll ask a few quick questions—reply to continue."
            await _send_progress(f"{intro}\n{prompt}")
            return ActionResult(intent=intent, success=True, detail="Setup wizard started")

        elif intent_type == "setup_wizard_progress":
            session = self._setup_wizards.active(str(guild.id), str(author_id))
            if session is None:
                session = self._setup_wizards.start(str(guild.id), str(author_id))
            answer = (requested_changes.get("answer") or "").strip()
            next_text, done = self._setup_wizards.advance(session, answer)
            if done:
                await _send_progress("Here’s the setup plan:")
            await _send_progress(next_text[:1800])
            return ActionResult(intent=intent, success=True, detail="Setup wizard complete" if done else "Setup wizard advanced")

        elif intent_type == "setup_wizard_cancel":
            cancelled = self._setup_wizards.cancel(str(guild.id), str(author_id))
            if cancelled:
                await _send_progress("Cancelled the setup wizard.")
                return ActionResult(intent=intent, success=True, detail="Setup wizard cancelled")
            await _send_progress("No active setup wizard to cancel.")
            return ActionResult(intent=intent, success=True, detail="No wizard running")

        elif intent_type == "draft_welcome_message":
            focus = (requested_changes.get("focus") or "").strip()
            tone = (requested_changes.get("tone") or "").strip()
            key_channels = (requested_changes.get("channels") or "").strip()
            server_name = getattr(guild, "name", "") or "this server"
            lines = [
                f"Hey there—welcome to {server_name}! 🎉",
                f"{'We’re all about ' + focus if focus else 'We’re happy you’re here.'}",
                "",
                "A few quick things to get you settled:",
            ]
            if key_channels:
                lines.append(f"• Start in {key_channels} for key info.")
            lines.append("• Say hi in #general and let folks know what you’re into.")
            lines.append("• Check the rules and stay kind—we keep things respectful.")
            lines.append("")
            lines.append("Need help? Ping the staff anytime.")
            if tone:
                lines.append(f"(Tone: {tone})")
            msg = "\n".join(lines)
            await _send_progress(msg[:1800])
            return ActionResult(intent=intent, success=True, detail="Drafted welcome message")

        elif intent_type == "add_faq":
            question = (requested_changes.get("question") or "").strip()
            answer = (requested_changes.get("answer") or "").strip()
            if not question or not answer:
                return ActionResult(intent=intent, success=False, detail="Missing FAQ question/answer")
            saved_q, saved_a = self._faqs.add(str(guild.id), question, answer, str(author_id))
            if not saved_q:
                return ActionResult(intent=intent, success=False, detail="FAQ add failed")
            await _send_progress(f"Saved FAQ: “{saved_q}”.")
            return ActionResult(intent=intent, success=True, detail="FAQ saved")

        elif intent_type == "answer_faq":
            question = (requested_changes.get("question") or "").strip()
            if not question:
                return ActionResult(intent=intent, success=False, detail="No FAQ question provided")
            answer = self._faqs.get(str(guild.id), question)
            if not answer:
                await _send_progress("I don’t have that FAQ yet.")
                return ActionResult(intent=intent, success=True, detail="FAQ missing")
            await _send_progress(answer[:1800])
            return ActionResult(intent=intent, success=True, detail="FAQ answered")

        elif intent_type == "list_faqs":
            faqs = self._faqs.list(str(guild.id))
            if not faqs:
                await _send_progress("No FAQs saved yet.")
                return ActionResult(intent=intent, success=True, detail="No FAQs")
            names = list(faqs.keys())
            shown = names[:20]
            extra = len(names) - len(shown)
            msg = "Saved FAQs: " + "; ".join(f"“{name}”" for name in shown)
            if extra > 0:
                msg += f" (+{extra} more)"
            await _send_progress(msg[:1800])
            return ActionResult(intent=intent, success=True, detail="Listed FAQs")

        elif intent_type == "remove_faq":
            question = (requested_changes.get("question") or "").strip()
            if not question:
                return ActionResult(intent=intent, success=False, detail="No FAQ question provided")
            removed = self._faqs.remove(str(guild.id), question)
            if removed:
                await _send_progress(f"Removed FAQ: “{question}”.")
                return ActionResult(intent=intent, success=True, detail="FAQ removed")
            await _send_progress("I couldn’t find that FAQ.")
            return ActionResult(intent=intent, success=True, detail="FAQ not found")

        elif intent_type == "list_macros":
            macros = self._macros.list(str(guild.id))
            if not macros:
                await _send_progress("No macros saved yet.")
                return ActionResult(intent=intent, success=True, detail="No macros")
            names = ", ".join([f"`{name}`" for name in macros.keys()])
            await _send_progress(f"Saved macros: {names}")
            return ActionResult(intent=intent, success=True, detail="Listed macros")

        elif intent_type == "save_macro":
            macro_name = (requested_changes.get("macro_name") or "").strip()
            macro_body = (requested_changes.get("macro_body") or "").strip()
            if not macro_name or not macro_body:
                return ActionResult(intent=intent, success=False, detail="Macro name/body missing")
            self._macros.save(str(guild.id), macro_name, macro_body, str(author_id))
            await _send_progress(f"Saved macro `{macro_name}`.")
            return ActionResult(intent=intent, success=True, detail="Saved macro")

        elif intent_type == "run_macro":
            macro_name = (requested_changes.get("macro_name") or "").strip()
            if not macro_name:
                return ActionResult(intent=intent, success=False, detail="Macro name missing")
            macro_body = self._macros.get(str(guild.id), macro_name)
            if not macro_body:
                await _send_progress(f"I don’t have a macro named `{macro_name}`.")
                return ActionResult(intent=intent, success=False, detail="Macro not found")
            stim = Stimulus(
                type="discord_message",
                source="macro",
                context={
                    "server_id": str(guild.id),
                    "channel_id": context_channel.id,
                    "author_id": author_id,
                    "message_id": intent.metadata.get("message_id", 0),
                    "content": macro_body,
                    "mentions_bot": True,
                    "mentioned_user_ids": [],
                    "channel_mentions": [],
                    "role_mentions": [],
                    "author_whitelisted": True,
                    "author_permissions": {"administrator": True, "manage_permissions": True},
                },
                salience=0.8,
            )
            parsed = parse_natural_language_intent(stim)
            if not parsed:
                await _send_progress(f"Macro `{macro_name}` didn’t map to a command.")
                return ActionResult(intent=intent, success=False, detail="Macro parse failed")
            try:
                macro_intent = ActionIntent(
                    type="tool_call",
                    target_id=context_channel.id,
                    payload={
                        "intent_type": parsed.intent_type,
                        "target_channel": parsed.target_channel,
                        "target_role": parsed.target_role,
                        "requested_changes": parsed.requested_changes,
                    },
                    metadata={
                        "author_id": author_id,
                        "guild_id": str(guild.id),
                        "reason": "macro_run",
                        "dry_run": parsed.dry_run,
                    },
                )
                await self.action_queue.put(macro_intent)
                await _send_progress(f"Running macro `{macro_name}`…")
                return ActionResult(intent=intent, success=True, detail="Macro executed")
            except Exception as exc:
                return ActionResult(intent=intent, success=False, detail=f"Macro execution failed: {exc}")

        elif intent_type == "last_action_explain":
            entry = self._action_journal.last(str(author_id))
            if not entry:
                await _send_progress("I don’t have a recent change recorded for you yet.")
                return ActionResult(intent=intent, success=True, detail="No last action recorded")
            age = max(0, time.time() - entry.timestamp)
            mins = int(age // 60)
            age_note = f"{mins} minute(s) ago" if mins else "just now"
            target_desc = ", ".join(f"{k}={v}" for k, v in entry.targets.items() if v) if entry.targets else ""
            msg = f"Last action: {entry.action_type} ({target_desc}) {age_note}."
            if entry.reversible:
                msg += " I can undo it if you want."
            else:
                msg += " It is not reversible."
            await _send_progress(msg[:1800])
            return ActionResult(intent=intent, success=True, detail="Reported last action")

        elif intent_type == "undo_last_action":
            entry = self._action_journal.pop_last_reversible(str(author_id))
            if not entry:
                await _send_progress("I don’t have anything to undo for you yet.")
                return ActionResult(intent=intent, success=False, detail="No reversible action")
            undo_result = await self._undo_action(entry, guild, context_channel, author_id)
            return undo_result

        elif intent_type == "delete_role":
            confirmed = bool(requested_changes.get("confirmed"))
            role_obj = None
            if role_id is not None:
                try:
                    role_obj = guild.get_role(int(role_id))
                except Exception:
                    role_obj = None
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    try:
                        role_obj = discord.utils.get(guild.roles, name=role_name)
                    except Exception:
                        role_obj = None
                    if role_obj is None:
                        for r in guild.roles:
                            if r.name.lower() == role_name.lower():
                                role_obj = r
                                break
            if role_obj is None:
                await _send_progress("Which role should I delete? Say `delete role \"RoleName\"` (or mention it).")
                return ActionResult(intent=intent, success=False, detail="Role not found")
            if role_obj == guild.default_role:
                return ActionResult(intent=intent, success=False, detail="Refusing to delete @everyone role")
            if getattr(role_obj, "managed", False):
                return ActionResult(intent=intent, success=False, detail="Refusing to delete a managed/integration role")
            if not confirmed and author_whitelisted_admin:
                confirmed = True
            if not confirmed:
                await _send_progress(
                    f'That will permanently delete the role `{role_obj.name}`. If you’re sure, say: `confirm delete role "{role_obj.name}"`.'
                )
                return ActionResult(intent=intent, success=True, detail="Confirmation requested")
            try:
                before_state = {"name": role_obj.name, "permissions": role_obj.permissions.value, "role_id": role_obj.id}
                await role_obj.delete(reason=f"Vyxen delete role by {author_id}")
                await _send_progress(f"Deleted role `{role_obj.name}`.")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"role_id": before_state["role_id"], "role_name": before_state["name"]},
                        before_state,
                        {"deleted": True},
                        reversible=True,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Deleted role {role_obj.name}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t delete that role: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Delete role failed: {exc}")

        elif intent_type == "ban_member":
            member_id = str(requested_changes.get("member_id") or "").strip()
            confirmed = bool(requested_changes.get("confirmed"))
            if not member_id:
                return ActionResult(intent=intent, success=False, detail="No member specified")
            if not confirmed and author_whitelisted_admin:
                confirmed = True
            if not confirmed:
                warning = ""
                try:
                    if str(member_id) == str(author_id) or str(member_id) in self.config.admin_user_ids:
                        warning = " (Heads up: that target looks like an admin/whitelisted user.)"
                except Exception:
                    pass
                await _send_progress(
                    f"Banning a member is destructive{warning}. If you’re sure, say: `confirm ban member {member_id}`."
                )
                return ActionResult(intent=intent, success=True, detail="Confirmation requested")
            try:
                target = guild.get_member(int(member_id))
                if target is None:
                    try:
                        target = await guild.fetch_member(int(member_id))
                    except Exception:
                        target = None
                ban_target = target if target is not None else discord.Object(id=int(member_id))
                await guild.ban(ban_target, reason=f"Vyxen ban by {author_id}", delete_message_days=0)
                await _send_progress(f"Banned `{member_id}`.")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"member_id": member_id},
                        None,
                        {"banned": True},
                        reversible=False,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Banned {member_id}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t ban that member: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Ban failed: {exc}")

        elif intent_type == "timeout_member":
            member_id = str(requested_changes.get("member_id") or "").strip()
            duration_seconds = requested_changes.get("duration_seconds")
            try:
                duration_seconds = int(duration_seconds)
            except Exception:
                duration_seconds = 600
            if not member_id:
                return ActionResult(intent=intent, success=False, detail="No member specified")
            member = guild.get_member(int(member_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except Exception:
                    member = None
            if member is None:
                await _send_progress("I couldn’t find that member in this server.")
                return ActionResult(intent=intent, success=False, detail="Member not found")
            try:
                from datetime import timedelta

                await member.timeout(timedelta(seconds=duration_seconds), reason=f"Vyxen timeout by {author_id}")
                mins = max(1, int(round(duration_seconds / 60)))
                await _send_progress(f"Timed out {member.mention} for ~{mins} minute(s).")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"member_id": member_id},
                        None,
                        {"timeout_seconds": duration_seconds},
                        reversible=False,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Timed out {member_id} {duration_seconds}s")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t timeout that member: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Timeout failed: {exc}")

        elif intent_type == "quarantine_member":
            member_id = str(requested_changes.get("member_id") or "").strip()
            if not member_id:
                return ActionResult(intent=intent, success=False, detail="No member specified")
            member = guild.get_member(int(member_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except Exception:
                    member = None
            if member is None:
                await _send_progress("I couldn’t find that member in this server.")
                return ActionResult(intent=intent, success=False, detail="Member not found")

            import re

            def _sanitize_channel_name(name: str) -> str:
                name = name.strip().lower()
                name = re.sub(r"[^a-z0-9 _-]", "", name)
                name = re.sub(r"\s+", "-", name)
                name = re.sub(r"-{2,}", "-", name).strip("-")
                return name or "channel"

            category_name = str(requested_changes.get("category_name") or "quarantine").strip()[:90]
            channel_name = str(requested_changes.get("channel_name") or "quarantine").strip()[:90]
            role_name = str(requested_changes.get("role_name") or "quarantine").strip()[:90]

            await _send_progress(f"Okay—setting up quarantine for {member.mention}. This can take a moment…")

            # Ensure role
            role_obj = discord.utils.get(guild.roles, name=role_name) or next(
                (r for r in guild.roles if r.name.lower() == role_name.lower()), None
            )
            if role_obj is None:
                try:
                    role_obj = await guild.create_role(name=role_name, reason=f"Vyxen quarantine by {author_id}")
                except Exception as exc:
                    self.tool_breaker.record_failure(str(exc))
                    return ActionResult(intent=intent, success=False, detail=f"Create quarantine role failed: {exc}")

            # Ensure category
            category_obj = discord.utils.get(guild.categories, name=category_name) or next(
                (c for c in guild.categories if c.name.lower() == category_name.lower()), None
            )
            if category_obj is None:
                try:
                    category_obj = await guild.create_category(name=category_name, reason=f"Vyxen quarantine by {author_id}")
                except Exception as exc:
                    self.tool_breaker.record_failure(str(exc))
                    return ActionResult(intent=intent, success=False, detail=f"Create quarantine category failed: {exc}")

            # Ensure quarantine text channel
            chan_sanitized = _sanitize_channel_name(channel_name)
            channel_obj = None
            try:
                existing = [ch for ch in guild.text_channels if ch.name == chan_sanitized]
                existing_in_cat = [ch for ch in existing if getattr(ch.category, "id", None) == category_obj.id]
                channel_obj = existing_in_cat[0] if existing_in_cat else (existing[0] if existing else None)
            except Exception:
                channel_obj = None
            if channel_obj is None:
                try:
                    channel_obj = await guild.create_text_channel(
                        name=chan_sanitized, category=category_obj, reason=f"Vyxen quarantine by {author_id}"
                    )
                except Exception as exc:
                    self.tool_breaker.record_failure(str(exc))
                    return ActionResult(intent=intent, success=False, detail=f"Create quarantine channel failed: {exc}")

            everyone = guild.default_role
            # Hide quarantine from everyone, allow quarantine role.
            try:
                ow_e_cat = category_obj.overwrites_for(everyone)
                ow_e_cat.view_channel = False
                await category_obj.set_permissions(everyone, overwrite=ow_e_cat, reason=f"Vyxen quarantine by {author_id}")
                ow_r_cat = category_obj.overwrites_for(role_obj)
                ow_r_cat.view_channel = True
                await category_obj.set_permissions(role_obj, overwrite=ow_r_cat, reason=f"Vyxen quarantine by {author_id}")

                ow_e = channel_obj.overwrites_for(everyone)
                ow_e.view_channel = False
                await channel_obj.set_permissions(everyone, overwrite=ow_e, reason=f"Vyxen quarantine by {author_id}")
                ow_r = channel_obj.overwrites_for(role_obj)
                ow_r.view_channel = True
                ow_r.send_messages = True
                ow_r.read_message_history = True
                await channel_obj.set_permissions(role_obj, overwrite=ow_r, reason=f"Vyxen quarantine by {author_id}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Quarantine created, but permissions failed: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Quarantine perms failed: {exc}")

            # Deny quarantine role access everywhere else.
            denied = 0
            for ch in guild.channels:
                if ch.id in {category_obj.id, channel_obj.id}:
                    continue
                try:
                    ow = ch.overwrites_for(role_obj)
                    if ow.view_channel is not False:
                        ow.view_channel = False
                        await ch.set_permissions(role_obj, overwrite=ow, reason=f"Vyxen quarantine by {author_id}")
                        denied += 1
                except Exception:
                    continue

            try:
                if role_obj not in member.roles:
                    await member.add_roles(role_obj, reason=f"Vyxen quarantine by {author_id}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Quarantine created, but I couldn’t assign the role: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Assign quarantine role failed: {exc}")

            await _send_progress(
                f"Done. Quarantine role `{role_obj.name}` assigned to {member.mention}. "
                f"They should only see {channel_obj.mention} now. (Updated {denied} channels.)"
            )
            self.tool_breaker.record_success()
            if not is_dry_run:
                self._record_action_journal(
                    author_id,
                    intent_type,
                    {"member_id": member_id, "role_id": role_obj.id, "channel_id": channel_obj.id},
                    None,
                    {"quarantined": True},
                    reversible=False,
                )
            return ActionResult(intent=intent, success=True, detail=f"Quarantined {member_id}")

        elif intent_type == "move_channel_to_category":
            category_name = (requested_changes.get("category_name") or "").strip()
            if not category_name:
                return ActionResult(intent=intent, success=False, detail="No category specified")

            category_obj = None
            try:
                category_obj = discord.utils.get(guild.categories, name=category_name)
            except Exception:
                category_obj = None
            if category_obj is None:
                for cat in guild.categories:
                    if cat.name.lower() == category_name.lower():
                        category_obj = cat
                        break
            if category_obj is None:
                await _send_progress(f"Couldn’t find a category named `{category_name}`.")
                return ActionResult(intent=intent, success=False, detail="Category not found")

            move_target = None
            try:
                if channel_id is not None and int(channel_id) != int(context_channel_id):
                    move_target = channel
            except Exception:
                move_target = None
            if move_target is None:
                channel_name = (requested_changes.get("channel_name") or "").strip()
                if not channel_name:
                    return ActionResult(intent=intent, success=False, detail="No channel specified")
                for ch in guild.channels:
                    if getattr(ch, "name", "").lower() == channel_name.lower():
                        move_target = ch
                        break

            if move_target is None:
                await _send_progress("Couldn’t find that channel to move. Try mentioning it like #channel.")
                return ActionResult(intent=intent, success=False, detail="Channel not found")

            try:
                if getattr(move_target, "category_id", None) == category_obj.id:
                    await _send_progress(f"{move_target.mention} is already under `{category_obj.name}`.")
                    self.tool_breaker.record_success()
                    return ActionResult(intent=intent, success=True, detail="No changes needed")
                before_state = {"from_category_id": getattr(move_target, "category_id", None)}
                await move_target.edit(category=category_obj, reason=f"Vyxen admin request by {author_id}")
                await _send_progress(f"Moved {move_target.mention} under `{category_obj.name}`.")
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"channel_id": move_target.id, "to_category_id": category_obj.id},
                        before_state,
                        {"to_category_id": category_obj.id},
                        reversible=True,
                    )
                return ActionResult(intent=intent, success=True, detail=f"Moved {move_target.name} -> {category_obj.name}")
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t move that channel: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Move channel failed: {exc}")

        elif intent_type == "lock_category":
            category_name = (requested_changes.get("category_name") or "").strip()
            if not category_name:
                return ActionResult(intent=intent, success=False, detail="No category specified")

            category_obj = None
            try:
                category_obj = discord.utils.get(guild.categories, name=category_name)
            except Exception:
                category_obj = None
            if category_obj is None:
                for cat in guild.categories:
                    if cat.name.lower() == category_name.lower():
                        category_obj = cat
                        break
            if category_obj is None:
                await _send_progress(f"Couldn’t find a category named `{category_name}`.")
                return ActionResult(intent=intent, success=False, detail="Category not found")

            role_obj = None
            if role_id is not None:
                try:
                    role_obj = guild.get_role(int(role_id))
                except Exception:
                    role_obj = None
            if role_obj is None:
                role_name = (requested_changes.get("role_name") or "").strip()
                if role_name:
                    try:
                        role_obj = discord.utils.get(guild.roles, name=role_name)
                    except Exception:
                        role_obj = None
                    if role_obj is None:
                        for r in guild.roles:
                            if r.name.lower() == role_name.lower():
                                role_obj = r
                                break
            if role_obj is None:
                await _send_progress("Which role should be allowed? Mention it like @Admin (or quote the role name).")
                return ActionResult(intent=intent, success=False, detail="Role not found")

            strict = bool(requested_changes.get("strict"))
            everyone = guild.default_role

            await _send_progress(
                f"Okay—locking category `{category_obj.name}` so only `{role_obj.name}` can view it."
                + (" (strict)" if strict else "")
            )

            changed = 0
            cleared = 0
            before_map: dict[int, dict[int, dict]] = {}
            try:
                ow_everyone = category_obj.overwrites_for(everyone)
                before_map[category_obj.id] = before_map.get(category_obj.id, {})
                before_map[category_obj.id][everyone.id] = self._serialize_overwrites(ow_everyone)
                if ow_everyone.view_channel is not False:
                    ow_everyone.view_channel = False
                    await category_obj.set_permissions(
                        everyone, overwrite=ow_everyone, reason=f"Vyxen lock category by {author_id}"
                    )
                    changed += 1

                ow_admin = category_obj.overwrites_for(role_obj)
                before_map[category_obj.id][role_obj.id] = self._serialize_overwrites(ow_admin)
                if ow_admin.view_channel is not True:
                    ow_admin.view_channel = True
                    await category_obj.set_permissions(
                        role_obj, overwrite=ow_admin, reason=f"Vyxen lock category by {author_id}"
                    )
                    changed += 1

                if strict:
                    for target, ow in (category_obj.overwrites or {}).items():
                        if not isinstance(target, discord.Role):
                            continue
                        if target.id in {everyone.id, role_obj.id}:
                            continue
                        before_map[category_obj.id][target.id] = self._serialize_overwrites(ow)
                        if ow.view_channel is True:
                            ow.view_channel = False
                            await category_obj.set_permissions(
                                target, overwrite=ow, reason=f"Vyxen lock category by {author_id}"
                            )
                            cleared += 1

                # Apply the same visibility rules to channels under the category.
                for ch in list(getattr(category_obj, "channels", []) or []):
                    try:
                        ow_e = ch.overwrites_for(everyone)
                        before_map[ch.id] = before_map.get(ch.id, {})
                        before_map[ch.id][everyone.id] = self._serialize_overwrites(ow_e)
                        if ow_e.view_channel is not False:
                            ow_e.view_channel = False
                            await ch.set_permissions(
                                everyone, overwrite=ow_e, reason=f"Vyxen lock category by {author_id}"
                            )
                        ow_r = ch.overwrites_for(role_obj)
                        before_map[ch.id][role_obj.id] = self._serialize_overwrites(ow_r)
                        if ow_r.view_channel is not True:
                            ow_r.view_channel = True
                            await ch.set_permissions(
                                role_obj, overwrite=ow_r, reason=f"Vyxen lock category by {author_id}"
                            )
                        if strict:
                            for target, ow in (ch.overwrites or {}).items():
                                if not isinstance(target, discord.Role):
                                    continue
                                if target.id in {everyone.id, role_obj.id}:
                                    continue
                                before_map[ch.id][target.id] = self._serialize_overwrites(ow)
                                if ow.view_channel is True:
                                    ow.view_channel = False
                                    await ch.set_permissions(
                                        target, overwrite=ow, reason=f"Vyxen lock category by {author_id}"
                                    )
                                    cleared += 1
                    except Exception:
                        continue

                await _send_progress(
                    f"Done. `{category_obj.name}` locked to `{role_obj.name}`. Changes={changed}, cleared_other_allows={cleared}."
                )
                self.tool_breaker.record_success()
                if not is_dry_run:
                    self._record_action_journal(
                        author_id,
                        intent_type,
                        {"category_id": category_obj.id, "role_id": role_obj.id},
                        {"overwrites": before_map},
                        {"locked": True},
                        reversible=True,
                    )
                return ActionResult(
                    intent=intent,
                    success=True,
                    detail=f"Locked {category_obj.name} to {role_obj.name} strict={strict} changed={changed} cleared={cleared}",
                )
            except Exception as exc:
                self.tool_breaker.record_failure(str(exc))
                await _send_progress(f"Couldn’t lock that category: {exc}")
                return ActionResult(intent=intent, success=False, detail=f"Lock category failed: {exc}")

        elif intent_type in {"bulk_setup", "server_setup", "create_category", "create_text_channel", "create_voice_channel", "create_role"}:
            # Create requested resources and apply simple permission overwrites when asked.
            import re

            def _sanitize_channel_name(name: str) -> str:
                name = name.strip().lower()
                # Hyphen must be last in the char class to avoid range parsing errors.
                name = re.sub(r"[^a-z0-9 _-]", "", name)
                name = re.sub(r"\s+", "-", name)
                name = re.sub(r"-{2,}", "-", name).strip("-")
                return name or "channel"

            category_name = requested_changes.get("category_name")
            channel_name = requested_changes.get("channel_name")
            role_name = requested_changes.get("role_name")
            channel_type = requested_changes.get("channel_type")
            perm_spec = requested_changes.get("permissions") or {}

            await _send_progress(
                f"Okay—working on it. I’m going to create what you asked for{(' (category/channel/role)' if intent_type in {'bulk_setup','server_setup'} else '')}."
            )

            category_obj = None
            category_created = False
            if category_name and intent_type in {"bulk_setup", "server_setup", "create_category", "create_text_channel", "create_voice_channel"}:
                category_name_clean = str(category_name).strip()[:90]
                try:
                    category_obj = discord.utils.get(guild.categories, name=category_name_clean)
                except Exception:
                    category_obj = None
                if category_obj is None:
                    for cat in guild.categories:
                        if cat.name.lower() == category_name_clean.lower():
                            category_obj = cat
                            break
                if category_obj is None:
                    try:
                        category_obj = await guild.create_category(
                            name=category_name_clean,
                            reason=f"Vyxen admin request by {author_id}",
                        )
                        category_created = True
                    except Exception as exc:
                        self.tool_breaker.record_failure(str(exc))
                        await _send_progress(f"Couldn’t create category `{category_name_clean}`: {exc}")
                        return ActionResult(intent=intent, success=False, detail=f"Create category failed: {exc}")

            channel_obj = None
            channel_created = False
            if channel_name and intent_type in {"bulk_setup", "server_setup", "create_text_channel", "create_voice_channel"}:
                wants_voice = (channel_type == "voice") or (intent_type == "create_voice_channel")
                if wants_voice:
                    voice_name = str(channel_name).strip()[:90]
                    try:
                        existing = [ch for ch in guild.voice_channels if ch.name.lower() == voice_name.lower()]
                        if category_obj is not None:
                            existing_in_cat = [
                                ch for ch in existing if getattr(ch.category, "id", None) == category_obj.id
                            ]
                            channel_obj = existing_in_cat[0] if existing_in_cat else (existing[0] if existing else None)
                        else:
                            channel_obj = existing[0] if existing else None
                    except Exception:
                        channel_obj = None

                    if channel_obj is None:
                        try:
                            channel_obj = await guild.create_voice_channel(
                                name=voice_name,
                                category=category_obj,
                                reason=f"Vyxen admin request by {author_id}",
                            )
                            channel_created = True
                        except Exception as exc:
                            self.tool_breaker.record_failure(str(exc))
                            await _send_progress(f"Couldn’t create voice channel `{voice_name}`: {exc}")
                            return ActionResult(intent=intent, success=False, detail=f"Create voice channel failed: {exc}")
                else:
                    sanitized = _sanitize_channel_name(str(channel_name))
                    try:
                        existing = [ch for ch in guild.text_channels if ch.name == sanitized]
                        if category_obj is not None:
                            existing_in_cat = [
                                ch for ch in existing if getattr(ch.category, "id", None) == category_obj.id
                            ]
                            channel_obj = existing_in_cat[0] if existing_in_cat else (existing[0] if existing else None)
                        else:
                            channel_obj = existing[0] if existing else None
                    except Exception:
                        channel_obj = None

                    if channel_obj is None:
                        try:
                            channel_obj = await guild.create_text_channel(
                                name=sanitized,
                                category=category_obj,
                                reason=f"Vyxen admin request by {author_id}",
                            )
                            channel_created = True
                        except Exception as exc:
                            self.tool_breaker.record_failure(str(exc))
                            await _send_progress(f"Couldn’t create text channel `{channel_name}`: {exc}")
                            return ActionResult(intent=intent, success=False, detail=f"Create channel failed: {exc}")

            role_obj = None
            role_created = False
            if role_name and intent_type in {"bulk_setup", "server_setup", "create_role"}:
                role_name_clean = str(role_name).strip()[:90]
                try:
                    role_obj = discord.utils.get(guild.roles, name=role_name_clean)
                except Exception:
                    role_obj = None
                if role_obj is None:
                    for r in guild.roles:
                        if r.name.lower() == role_name_clean.lower():
                            role_obj = r
                            break
                if role_obj is None:
                    try:
                        role_obj = await guild.create_role(
                            name=role_name_clean,
                            reason=f"Vyxen admin request by {author_id}",
                        )
                        role_created = True
                    except Exception as exc:
                        self.tool_breaker.record_failure(str(exc))
                        await _send_progress(f"Couldn’t create role `{role_name_clean}`: {exc}")
                        return ActionResult(intent=intent, success=False, detail=f"Create role failed: {exc}")

            if perm_spec and role_obj and channel_obj:
                try:
                    overwrites = channel_obj.overwrites_for(role_obj)
                    applied_allow: list[str] = []
                    applied_deny: list[str] = []
                    applied_clear: list[str] = []
                    for perm_name, perm_value in perm_spec.items():
                        if not isinstance(perm_name, str) or not hasattr(overwrites, perm_name):
                            continue
                        if perm_value is None:
                            setattr(overwrites, perm_name, None)
                            applied_clear.append(perm_name)
                        elif isinstance(perm_value, bool):
                            setattr(overwrites, perm_name, perm_value)
                            (applied_allow if perm_value else applied_deny).append(perm_name)
                        else:
                            coerced = bool(perm_value)
                            setattr(overwrites, perm_name, coerced)
                            (applied_allow if coerced else applied_deny).append(perm_name)
                    await channel_obj.set_permissions(
                        role_obj,
                        overwrite=overwrites,
                        reason=f"Vyxen admin request by {author_id}",
                    )
                except Exception as exc:
                    self.tool_breaker.record_failure(str(exc))
                    await _send_progress(f"Created things, but couldn’t set permissions: {exc}")
                    return ActionResult(intent=intent, success=False, detail=f"Set permissions failed: {exc}")

            parts: list[str] = []
            if category_obj is not None:
                parts.append(
                    f"Category: `{category_obj.name}`" + ("" if category_created else " (existing)")
                )
            if channel_obj is not None:
                parts.append(
                    f"Channel: {channel_obj.mention}" + ("" if channel_created else " (existing)")
                )
            if role_obj is not None:
                parts.append(
                    f"Role: `{role_obj.name}`" + ("" if role_created else " (existing)")
                )
            if perm_spec and role_obj and channel_obj:
                allow_list = [k for k, v in perm_spec.items() if v is True]
                deny_list = [k for k, v in perm_spec.items() if v is False]
                clear_list = [k for k, v in perm_spec.items() if v is None]

                def _clip(items: list[str], limit: int = 6) -> str:
                    if len(items) <= limit:
                        return ", ".join(items)
                    return ", ".join(items[:limit]) + f", +{len(items) - limit}"

                perm_parts: list[str] = []
                if allow_list:
                    perm_parts.append(f"allow: {_clip(allow_list)}")
                if deny_list:
                    perm_parts.append(f"deny: {_clip(deny_list)}")
                if clear_list:
                    perm_parts.append(f"clear: {_clip(clear_list)}")
                parts.append("Permissions: " + ("; ".join(perm_parts) if perm_parts else "set overwrites"))

            await _send_progress("Done. " + (" | ".join(parts) if parts else "No changes were needed."))
            self.tool_breaker.record_success()
            if not is_dry_run:
                if category_created:
                    self._record_action_journal(
                        author_id,
                        "create_category",
                        {"category_id": category_obj.id if category_obj else None, "name": category_obj.name if category_obj else category_name},
                        None,
                        {"category_id": category_obj.id if category_obj else None},
                        reversible=True,
                    )
                if channel_created and channel_obj is not None:
                    self._record_action_journal(
                        author_id,
                        "create_voice_channel" if wants_voice else "create_text_channel",
                        {"channel_id": channel_obj.id, "name": channel_obj.name},
                        None,
                        {"channel_id": channel_obj.id, "type": "voice" if wants_voice else "text", "category_id": getattr(channel_obj, "category_id", None)},
                        reversible=True,
                    )
                if role_created and role_obj is not None:
                    self._record_action_journal(
                        author_id,
                        "create_role",
                        {"role_id": role_obj.id, "name": role_obj.name},
                        None,
                        {"role_id": role_obj.id, "permissions": role_obj.permissions.value},
                        reversible=True,
                    )
            return ActionResult(intent=intent, success=True, detail="; ".join(parts) if parts else "No-op")

        return ActionResult(intent=intent, success=False, detail="Unsupported tool intent")

    async def _enqueue_stimulus(self, stimulus: Stimulus) -> None:
        try:
            self.stimulus_queue.put_nowait(stimulus)
        except asyncio.QueueFull:
            self.logger.warning("Stimulus queue full; dropping %s", stimulus.type)

    async def _get_channel(self, channel_id: Optional[int]) -> Optional[discord.TextChannel]:
        if channel_id is None:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as exc:
                print(f"[ACTION-ERROR] fetch_channel failed for {channel_id}: {exc}")
                return None
        return channel

    def _can_send(self, channel: discord.TextChannel) -> bool:
        me = channel.guild.me if channel.guild else None
        if not me:
            return True
        perms = channel.permissions_for(me)
        return perms.send_messages

    def _calculate_salience(self, message: discord.Message) -> float:
        salience = 0.4
        if self.user and self.user in message.mentions:
            salience += 0.3
        if message.attachments:
            salience += 0.2
        if len(message.content) > 150:
            salience += 0.1
        return min(1.0, salience)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable required.")

    pre_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"[MEMDBG] start main: {pre_mem:.1f} MB", flush=True)

    config = RuntimeConfig.from_env()
    stimulus_queue: asyncio.Queue = asyncio.Queue(maxsize=config.stimulus_queue_limit)
    action_queue: asyncio.Queue = asyncio.Queue(maxsize=config.action_queue_limit)

    state = InternalState(safe_mode=config.safe_mode_default)
    memory = CausalMemory(config, allow_writes=not config.safe_mode_default)
    identity = IdentityCore.load(config, allow_persistence=not config.safe_mode_default)
    governor = Governor(identity, memory)
    sessions = SessionStore(ttl_seconds=config.session_ttl_seconds)
    cognition = CognitionLoop(
        config=config,
        state=state,
        memory=memory,
        identity=identity,
        governor=governor,
        sessions=sessions,
        stimulus_queue=stimulus_queue,
        action_queue=action_queue,
    )

    adapter = DiscordAdapter(
        config=config,
        cognition=cognition,
        stimulus_queue=stimulus_queue,
        action_queue=action_queue,
    )

    mid_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"[MEMDBG] post-setup: {mid_mem:.1f} MB", flush=True)

    def _log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[TASK-ERROR] {task.get_name()}: {exc}", file=sys.stderr, flush=True)

    # Kick off cognition loop separately to keep running regardless of Discord events
    cognition_task = asyncio.create_task(cognition.start(), name="cognition_loop")
    mem_watch_task = asyncio.create_task(_memory_watch(state), name="memory_watch")
    cognition_task.add_done_callback(_log_task_failure)
    mem_watch_task.add_done_callback(_log_task_failure)
    after_cog = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"[MEMDBG] after cognition start: {after_cog:.1f} MB", flush=True)
    try:
        await adapter.start(token=token)
    finally:
        cognition.running = False
        cognition_task.cancel()
        mem_watch_task.cancel()
        await asyncio.gather(cognition_task, mem_watch_task, return_exceptions=True)


def _env_truthy(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    val = val.strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


async def _memory_watch(state: InternalState, interval: float = 1.0, probe_threshold_mb: float = 600.0) -> None:
    enable_tracemalloc = _env_truthy("VYXEN_MEMWATCH_TRACEMALLOC", default=False)
    try:
        import tracemalloc  # type: ignore

        if enable_tracemalloc:
            tracemalloc.start()
    except Exception:
        tracemalloc = None  # type: ignore
    try:
        import faulthandler
    except Exception:
        faulthandler = None  # type: ignore
    logged = False
    last_reported = 0.0
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        try:
            with open("/proc/self/statm", "r") as fh:
                parts = fh.read().split()
                rss_pages = int(parts[1]) if len(parts) > 1 else 0
                rss_mb = rss_pages * (resource.getpagesize() / (1024 * 1024))
        except Exception:
            try:
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            except Exception:
                continue
        if rss_mb - last_reported >= 50:
            last_reported = rss_mb
            print(f"[MEMWATCH] rss={rss_mb:.1f} MB safe_mode={state.safe_mode}", flush=True)
        if rss_mb > probe_threshold_mb and not logged:
            logged = True
            print(f"[MEMPROBE] rss={rss_mb:.1f} MB safe_mode={state.safe_mode}", flush=True)
            if faulthandler is not None:
                try:
                    faulthandler.dump_traceback(file=sys.stdout)
                except Exception:
                    pass
                if tracemalloc and getattr(tracemalloc, "is_tracing", lambda: False)():
                    snapshot = tracemalloc.take_snapshot()
                    stats = snapshot.statistics("lineno")[:6]
                    for stat in stats:
                        frame = stat.traceback[0]
                        line_src = getattr(frame, "line", None)
                        if line_src is None:
                            try:
                                import linecache

                                line_src = (linecache.getline(frame.filename, frame.lineno) or "").strip()
                            except Exception:
                                line_src = ""
                        else:
                            line_src = line_src.strip()
                        print(
                            f"[MEMPROBE] {stat.size / 1024:.1f} KB @ {frame.filename}:{frame.lineno} {line_src}",
                            flush=True,
                        )

if __name__ == "__main__":
    asyncio.run(main())
