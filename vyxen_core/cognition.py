import asyncio
import concurrent.futures
import os
import time
from typing import List, Optional, Dict, Any
try:
    import resource
except ImportError:
    resource = None  # type: ignore

from .actions import ActionIntent, ActionResult, ActionAuditor
from .audit import build_logger
from .config import RuntimeConfig
from .governor import Governor
from .identity import IdentityCore
from .memory import CausalMemory, extract_topics, clamp01
from .conversation import SessionStore, ConversationSession
from .health import scan_pm2_logs
from .safety import CircuitBreaker, SafetyDiagnostics
from . import llm
from .realities.tools import ToolsReality
from .realities import (
    ModerationReality,
    NarrativeReality,
    RealityOutput,
    SocialReality,
    StrategicReality,
)
from .state import InternalState
from .stimuli import Stimulus


class CognitionLoop:
    def __init__(
        self,
        config: RuntimeConfig,
        state: InternalState,
        memory: CausalMemory,
        identity: IdentityCore,
        governor: Governor,
        sessions: SessionStore,
        stimulus_queue: asyncio.Queue,
        action_queue: asyncio.Queue,
    ):
        self.config = config
        self.state = state
        self.memory = memory
        self.identity = identity
        self.governor = governor
        self.sessions = sessions
        self.stimulus_queue: asyncio.Queue[Stimulus] = stimulus_queue
        self.action_queue: asyncio.Queue[ActionIntent] = action_queue
        self.logger = build_logger(config)
        self.auditor = ActionAuditor()
        self.safety = SafetyDiagnostics()
        self.pm2_breaker = CircuitBreaker("pm2_logs", threshold=3, window_seconds=300.0, cooldown_seconds=900.0)
        self.realities = [
            SocialReality(config=config),
            ModerationReality(dry_run=config.automod_dry_run),
            NarrativeReality(),
            StrategicReality(),
            ToolsReality(enabled=config.tools_enabled, dry_run=config.tools_dry_run),
        ]
        # Keep thread usage bounded and isolated from other components (LLM/tooling).
        interpret_workers = max(1, min(self.config.max_tasks_per_tick, len(self.realities), 8))
        self._interpret_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=interpret_workers, thread_name_prefix="vyxen-interpret"
        )
        self._maintenance_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vyxen-maint"
        )
        self.running = False
        self.last_tick = time.monotonic()
        self.last_health_scan = 0.0
        self.tick_interval_override: Optional[float] = None
        self.log_ingestion_disabled = False
        self.state.safe_mode = config.safe_mode_default
        self.health_task: Optional[asyncio.Task] = None
        self.maintenance_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.running = True
        if self.health_task is None:
            self.health_task = asyncio.create_task(self._health_monitor())
        if self.maintenance_task is None:
            self.maintenance_task = asyncio.create_task(self._maintenance_loop())
        try:
            while self.running:
                tick_start = time.monotonic()
                self.state.llm_calls_remaining = self.config.max_llm_calls_per_tick
                try:
                    await asyncio.wait_for(
                        self._tick(tick_start),
                        timeout=max(0.05, self.config.tick_budget_ms / 1000.0 + 0.05),
                    )
                except asyncio.TimeoutError:
                    self._record_overrun("tick_timeout")
                except asyncio.CancelledError:
                    self.running = False
                    raise
                except Exception as exc:
                    self.state.safe_mode = True
                    try:
                        self.memory.allow_writes = False
                    except Exception:
                        pass
                    self.safety.last_overrun_reason = "tick_exception"
                    self.logger.exception("Cognition tick crashed; entering Safe Mode: %s", exc)
                    continue
                elapsed = time.monotonic() - tick_start
                self._watchdog(elapsed)
                await asyncio.sleep(max(0.0, self._current_tick_interval() - elapsed))
        finally:
            self.running = False
            if self.health_task:
                self.health_task.cancel()
            if self.maintenance_task:
                self.maintenance_task.cancel()
            try:
                self._interpret_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            try:
                self._maintenance_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    async def _tick(self, tick_start: float) -> None:
        deadline = tick_start + self.config.tick_budget_ms / 1000.0
        now = time.monotonic()
        dt = now - self.last_tick
        self.last_tick = now
        self.state.decay(dt)
        self._update_status_snapshot()

        # Expire sessions on each tick to allow reflection even without new stimuli
        ended_sessions = self.sessions.expire_stale()
        for session, reason in ended_sessions:
            self._reflect_session_end(session, reason)

        if time.monotonic() > deadline:
            self._record_overrun("decay_budget_exhausted")
            return

        stimuli = await self._perceive(self.config.max_stimuli_per_tick)
        for stim in stimuli:
            if time.monotonic() > deadline:
                self._record_overrun("tick_budget_exceeded")
                return
            channel_id = stim.context.get("channel_id")
            if channel_id is not None:
                self.state.last_channel_id = channel_id
            server_id = stim.context.get("server_id")
            if server_id is not None:
                self.state.last_server_id = server_id
            self.state.update_on_stimulus(stim.type, stim.salience)
            mention = bool(stim.context.get("mentions_bot"))
            session_active = bool(stim.context.get("session_active"))
            directed = stim.routing == "directed" or mention or session_active
            try:
                if stim.type in {"discord_message", "attachment"} and not directed:
                    interpretations = []
                else:
                    interpretations = await self._interpret(stim, deadline)
            except Exception as exc:
                self.logger.warning("Interpretation failed for %s: %s", stim.type, exc)
                interpretations = []
            if time.monotonic() > deadline:
                self._record_overrun("interpret_budget_exceeded")
                return
            try:
                decision = self._decide(stim, interpretations)
            except Exception as exc:
                self.logger.warning("Decision step failed for %s: %s", stim.type, exc)
                continue
            try:
                result = await self._act(decision)
            except Exception as exc:
                self.logger.warning("Action dispatch failed for %s: %s", stim.type, exc)
                continue
            try:
                self._reflect(stim, interpretations, decision, result)
            except Exception as exc:
                self.state.safe_mode = True
                self.logger.warning("Reflection failed; forcing Safe Mode: %s", exc)

    async def _perceive(self, max_items: int) -> List[Stimulus]:
        stimuli: List[Stimulus] = []
        for _ in range(max_items):
            try:
                stimuli.append(self.stimulus_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not stimuli and self.stimulus_queue.qsize() > 0:
            self._record_overrun("stimulus_queue_backlog")

        if stimuli and any(s.type != "silence" for s in stimuli):
            try:
                print(f"[COG] draining {len(stimuli)} stimuli (queue now {self.stimulus_queue.qsize()})")
            except Exception:
                pass
            if len(stimuli) > self.config.max_stimuli_per_tick:
                stimuli = stimuli[: self.config.max_stimuli_per_tick]

        now = time.time()
        if not stimuli and now - self.state.last_perceived >= self.config.silence_gap_seconds:
            # Emit silence stimulus with decaying salience based on inactivity
            silence_salience = max(0.1, min(0.7, (now - self.state.last_perceived) / 60))
            stimuli.append(
                Stimulus(
                    type="silence",
                    source="vyxen_core",
                    context={
                        "channel_id": self.state.last_channel_id,
                        "server_id": self.state.last_server_id or "global",
                    },
                    salience=silence_salience,
                    routing="system",
                )
            )
            self.state.last_perceived = now

        routed_stimuli: List[Stimulus] = []
        for stim in stimuli:
            try:
                # Route and manage sessions for user-facing events
                if stim.type in {"silence", "discord_member_join", "discord_member_leave"}:
                    stim.routing = "system"
                    stim.context.setdefault("session_active", False)
                else:
                    routing, session, ended = self.sessions.route_stimulus(stim)
                    for ended_session, reason in ended:
                        self._reflect_session_end(ended_session, reason)
                    stim.routing = routing
                    stim.context["session_active"] = routing == "directed"
                    if session:
                        stim.context["session_start"] = session.session_start
                        stim.context["session_expires_at"] = session.expires_at
            except Exception as exc:
                self.logger.warning("Session routing failed: %s", exc)
                stim.routing = "ambient"
                stim.context["session_active"] = False
                routed_stimuli.append(stim)
                continue
            if stim.type != "silence":
                try:
                    print(
                        f"[ROUTE] type={stim.type} routing={stim.routing} mention={stim.context.get('mentions_bot')} session={stim.context.get('session_active')}"
                    )
                except Exception:
                    pass

            # Attach memory echoes as contextual anchors
            if stim.type == "silence" or stim.routing in {"system", "ambient"}:
                stim.context["memory_echoes"] = []
            else:
                echoes = self.memory.echoes(stim.context.get("server_id", "global"), stim)

                def _prune_echo_context(ctx: dict) -> dict:
                    # Avoid recursive growth/circular references in contexts
                    trimmed = dict(ctx)
                    trimmed.pop("memory_echoes", None)
                    return trimmed

                stim.context["memory_echoes"] = [_prune_echo_context(e.context) for e in echoes]
            routed_stimuli.append(stim)
        return routed_stimuli

    async def _interpret(self, stimulus: Stimulus, deadline: float) -> List[RealityOutput]:
        realities = self.realities[: self.config.max_tasks_per_tick]
        if not realities:
            return []

        loop = asyncio.get_running_loop()
        timeout = max(0.01, deadline - time.monotonic())
        futures = [
            loop.run_in_executor(
                self._interpret_executor,
                reality.interpret,
                stimulus,
                self.state,
                self.memory,
                self.identity,
            )
            for reality in realities
        ]
        done: set[asyncio.Future] = set()
        pending: set[asyncio.Future] = set()
        try:
            done, pending = await asyncio.wait(futures, timeout=timeout)
        except Exception as exc:
            self.logger.warning("Reality interpretation scheduling error: %s", exc)
            return []

        outputs: List[RealityOutput] = []
        for fut in done:
            try:
                outputs.append(fut.result())
            except Exception as exc:
                self.logger.warning("Reality interpretation error: %s", exc)

        if pending:
            for fut in pending:
                try:
                    fut.cancel()
                except Exception:
                    pass
            self._record_overrun("interpret_timeout")
        return outputs

    def _decide(
        self, stimulus: Stimulus, interpretations: List[RealityOutput]
    ) -> "GovernorDecisionWrapper":
        mention = bool(stimulus.context.get("mentions_bot"))
        session_active = bool(stimulus.context.get("session_active"))
        directed = stimulus.routing == "directed" or mention or session_active

        audit_ctx: Dict[str, Any] = {}
        try:
            ctx_trimmed: Dict[str, Any] = {k: v for k, v in stimulus.context.items() if k not in {"memory_echoes"}}
            content_full = ctx_trimmed.pop("content", None)
            if isinstance(content_full, str) and content_full:
                ctx_trimmed["content_len"] = len(content_full)
                ctx_trimmed["content_snippet"] = content_full[:160] + ("…" if len(content_full) > 160 else "")
            audit_ctx = {
                "type": stimulus.type,
                "source": stimulus.source,
                "context": ctx_trimmed,
                "salience": stimulus.salience,
                "timestamp": stimulus.timestamp,
            }
        except Exception:
            audit_ctx = {
                "type": stimulus.type,
                "source": stimulus.source,
                "context": {},
                "salience": stimulus.salience,
                "timestamp": stimulus.timestamp,
            }

        decision = self.governor.deliberate(
            server_id=stimulus.context.get("server_id", "global"),
            realities=interpretations,
            directed=directed,
        )
        if decision is None:
            fallback = ActionIntent(type="observe", target_id=None, payload={}, metadata={})
            wrapper = GovernorDecisionWrapper(
                intent=fallback,
                interpretations=interpretations,
                confidence=0.3,
                risk=0.1,
                rationale="Default observe when no decision produced.",
            )
        else:
            wrapper = GovernorDecisionWrapper(
                intent=decision.action,
                interpretations=interpretations,
                confidence=decision.confidence,
                risk=decision.risk,
                rationale=decision.rationale,
            )
        if self.state.safe_mode and wrapper.intent.type not in {"reply", "send_message", "observe"}:
            safe_wrapper = GovernorDecisionWrapper(
                intent=ActionIntent(type="observe", target_id=None, payload={}, metadata={"reason": "safe_mode_block"}),
                interpretations=interpretations,
                confidence=0.3,
                risk=0.05,
                rationale="Safe Mode limits actions to read-only replies.",
            )
            audit_context = {
                "stimulus": {
                    **audit_ctx,
                },
                "realities": [o.to_dict(include_metadata=False) for o in interpretations],
                "governor_choice": {
                    "decision": safe_wrapper.rationale,
                    "confidence": safe_wrapper.confidence,
                    "risk": safe_wrapper.risk,
                    "session_active": session_active,
                    "mentioned": mention,
                    "routing": stimulus.routing,
                },
            }
            if stimulus.type != "silence":
                safe_wrapper.intent.metadata["audit_context"] = audit_context
            return safe_wrapper

        audit_context = {
            "stimulus": {
                **audit_ctx,
            },
            "realities": [o.to_dict(include_metadata=False) for o in interpretations],
            "governor_choice": {
                "decision": wrapper.rationale,
                "confidence": wrapper.confidence,
                "risk": wrapper.risk,
                "session_active": session_active,
                "mentioned": mention,
                "routing": stimulus.routing,
            },
        }
        if stimulus.type != "silence":
            wrapper.intent.metadata["audit_context"] = audit_context
        return wrapper

    async def _act(self, decision: "GovernorDecisionWrapper") -> ActionResult:
        if self.state.safe_mode and decision.intent.type not in {"observe", "reply", "send_message"}:
            fallback_intent = ActionIntent(
                type="observe", target_id=None, payload={}, metadata={"reason": "safe_mode_block"}
            )
            try:
                self.action_queue.put_nowait(fallback_intent)
                result = ActionResult(
                    intent=fallback_intent,
                    success=True,
                    detail="Safe Mode enforced; non-text action blocked",
                )
            except asyncio.QueueFull:
                self._record_overrun("action_queue_full")
                result = ActionResult(
                    intent=fallback_intent,
                    success=False,
                    detail="Action queue saturated",
                )
            self.auditor.record(result)
            return result
        if decision.intent.type != "observe":
            try:
                print(
                    f"[DECIDE] action={decision.intent.type} target={decision.intent.target_id} safe={self.state.safe_mode}"
                )
            except Exception:
                pass
        try:
            self.action_queue.put_nowait(decision.intent)
        except asyncio.QueueFull:
            self._record_overrun("action_queue_full")
            result = ActionResult(
                intent=decision.intent,
                success=False,
                detail="Action queue saturated",
            )
            self.auditor.record(result)
            return result
        else:
            result = ActionResult(
                intent=decision.intent,
                success=True,
                detail="Queued for adapter execution",
            )
            self.auditor.record(result)
            return result

    def _record_overrun(self, reason: str) -> None:
        self.state.overrun_note = reason
        self.state.safe_mode = True
        try:
            self.memory.allow_writes = False
        except Exception:
            pass
        self.safety.last_overrun_reason = reason
        self.logger.warning("Tick overrun: %s", reason)

    def _current_tick_interval(self) -> float:
        base = (
            self.config.safe_mode_tick_interval_seconds
            if self.state.safe_mode
            else self.config.tick_interval_seconds
        )
        if self.tick_interval_override:
            base = max(base, self.tick_interval_override)
        return base

    def _watchdog(self, tick_elapsed: float) -> None:
        overload = False
        reasons: list[str] = []
        try:
            load_avg = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            load_per_cpu = load_avg / max(1, cpu_count)
            if load_per_cpu > self.config.watchdog_cpu_load:
                overload = True
                reasons.append(f"cpu_load={load_avg:.2f}/cpu={load_per_cpu:.2f}")
        except Exception:
            pass

        if resource:
            try:
                usage = resource.getrusage(resource.RUSAGE_SELF)
                mem_mb = usage.ru_maxrss / 1024
                if mem_mb > self.config.watchdog_memory_mb:
                    overload = True
                    reasons.append(f"rss_mb={mem_mb:.0f}")
            except Exception:
                pass

        expected = self._current_tick_interval()
        loop_lag = max(0.0, tick_elapsed - expected)
        if loop_lag > self.config.watchdog_event_loop_lag:
            overload = True
            reasons.append(f"loop_lag={loop_lag:.3f}")

        if self.stimulus_queue.qsize() > self.config.watchdog_queue_depth:
            overload = True
            reasons.append(f"stimuli_backlog={self.stimulus_queue.qsize()}")

        if overload:
            self.state.safe_mode = True
            try:
                self.memory.allow_writes = False
            except Exception:
                pass
            self.tick_interval_override = self.config.watchdog_safe_tick_interval
            note = ";".join(reasons)
            self.state.watchdog_note = note
            self.safety.last_watchdog_reason = note
            self.logger.warning("Watchdog throttle engaged: %s", note)

    def _update_status_snapshot(self) -> None:
        uptime = time.monotonic() - self.state.start_time
        pm2_reason = self.pm2_breaker.reason if self.pm2_breaker.tripped else ""
        mem_tripped, mem_reason = (False, "")
        if hasattr(self.memory, "breaker_status"):
            try:
                mem_tripped, mem_reason = self.memory.breaker_status()
            except Exception:
                mem_tripped, mem_reason = False, ""
        llm_tripped, llm_reason = (False, "")
        try:
            llm_tripped, llm_reason = llm.breaker_status()
        except Exception:
            llm_tripped, llm_reason = False, ""
        mem_info = {}
        try:
            mem_info = self.memory.size_info()
            self.state.memory_last_rotation = mem_info.get("last_rotation_ts")
            self.state.memory_hot_mb = mem_info.get("hot_mb", 0.0)
            self.state.memory_warm_mb = mem_info.get("warm_mb", 0.0)
            self.state.memory_disabled_reason = mem_info.get("disabled_reason", "")
        except Exception:
            pass
        snapshot: Dict[str, Any] = {
            "safe_mode": self.state.safe_mode,
            "tick_interval": self._current_tick_interval(),
            "uptime_seconds": uptime,
            "overrun": self.safety.last_overrun_reason,
            "watchdog": self.safety.last_watchdog_reason,
            "log_ingest_disabled": self.safety.log_ingest_disabled,
            "pm2_breaker": pm2_reason,
            "memory_breaker": mem_reason if mem_tripped else "",
            "llm_breaker": llm_reason if llm_tripped else "",
            "tools_enabled": self.config.tools_enabled and not self.state.safe_mode,
            "automod_enforcing": False,
            "automod_dry_run": True,
            "max_llm_calls": self.config.max_llm_calls_per_tick,
            "memory_hot_mb": mem_info.get("hot_mb", 0.0),
            "memory_warm_mb": mem_info.get("warm_mb", 0.0),
            "memory_last_rotation": mem_info.get("last_rotation_ts"),
            "memory_disabled_reason": mem_info.get("disabled_reason", ""),
        }
        try:
            snapshot["pm2_restart_count"] = int(os.getenv("PM2_RESTART_TIME", "0") or 0)
        except Exception:
            snapshot["pm2_restart_count"] = None
        self.state.status_snapshot = snapshot

    async def _health_monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(self.config.health_scan_interval, self.config.pm2_scan_min_interval))
            except asyncio.CancelledError:
                return
            if not self.running:
                return
            if self.state.safe_mode or self.log_ingestion_disabled or not self.config.pm2_ingestion_enabled:
                continue
            if not self.pm2_breaker.allow():
                self.log_ingestion_disabled = True
                self.safety.log_ingest_disabled = True
                self.logger.warning("PM2 log ingestion breaker tripped; disabling reader.")
                continue

            start = time.time()
            try:
                health_stimuli = scan_pm2_logs(
                    self.config,
                    lines=self.config.pm2_max_lines,
                    timeout_seconds=self.config.log_ingest_timeout_seconds,
                )
                self.pm2_breaker.record_success()
            except Exception as exc:
                self.pm2_breaker.record_failure(str(exc))
                continue
            duration = time.time() - start
            if duration > self.config.log_ingest_timeout_seconds:
                self.log_ingestion_disabled = True
                self.safety.log_ingest_disabled = True
                self.state.safe_mode = True
                self.logger.warning("Disabled log ingestion after slow read (%.3fs)", duration)
                continue

            for stim in health_stimuli:
                try:
                    self.stimulus_queue.put_nowait(stim)
                except asyncio.QueueFull:
                    self._record_overrun("health_stimulus_queue_full")

    async def _maintenance_loop(self) -> None:
        """
        Off-tick maintenance to keep memory small and safe.
        """
        while True:
            try:
                await asyncio.sleep(self.config.memory_maintenance_interval)
            except asyncio.CancelledError:
                return
            if not self.running:
                return
            # Run maintenance off the event loop thread to avoid blocking ticks
            try:
                loop = asyncio.get_running_loop()
                report = await loop.run_in_executor(self._maintenance_executor, self.memory.maintain)
                if report.get("disabled"):
                    self.state.safe_mode = True
            except Exception as exc:
                self.logger.warning("Maintenance loop failed: %s", exc)

    def _reflect(
        self,
        stimulus: Stimulus,
        interpretations: List[RealityOutput],
        decision: "GovernorDecisionWrapper",
        result: ActionResult,
    ) -> None:
        if self.state.safe_mode and not self.memory.allow_writes:
            return
        if stimulus.type in {"discord_message", "attachment"} and stimulus.routing == "ambient":
            # Avoid recording ambient chatter into memory; keep memory intentional and bounded.
            return
        if stimulus.type == "silence":
            return
        confidence_delta = decision.confidence - decision.risk
        outcome_score = 1.0 if result.success else -0.5
        self.identity.adjust_from_outcome(outcome_score * confidence_delta)

        if stimulus.type == "server_snapshot":
            self.memory.record_server_snapshot(
                server_id=stimulus.context.get("server_id", "global"),
                snapshot={
                    "roles": stimulus.context.get("roles", []),
                    "channels": stimulus.context.get("channels", []),
                    "member_count": stimulus.context.get("member_count"),
                    "timestamp": stimulus.timestamp,
                },
            )
        if stimulus.type == "server_event":
            self.memory.record_server_event(
                server_id=stimulus.context.get("server_id", "global"),
                event_type=stimulus.context.get("event_type", "unknown"),
                data=stimulus.context.get("data", {}),
            )

        server_id = stimulus.context.get("server_id", "global")
        author_id = stimulus.context.get("author_id")

        if stimulus.type == "discord_message" and author_id is not None:
            content = stimulus.context.get("content", "")
            topics = extract_topics(content)
            mentioned_ids = [
                str(uid) for uid in stimulus.context.get("mentioned_user_ids", [])
            ]
            participants = [str(author_id), *mentioned_ids]

            # Lightweight profiling heuristics
            verbosity_delta = clamp01(len(content) / 400) * 0.1 - 0.02
            humor_delta = 0.05 if any(
                marker in content.lower() for marker in ["lol", "lmao", "haha", "😂"]
            ) else -0.01
            tone_delta = 0.03 if "?" in content else -0.01
            success_delta = outcome_score * 0.05
            warmth_delta = 0.04 if any(word in content.lower() for word in ["thanks", "appreciate", "please"]) else -0.01
            formality_delta = 0.03 if any(word in content.lower() for word in ["please", "thank you", "regards"]) else -0.01
            precision_delta = 0.04 if any(token in content for token in [":", "-", "->"]) else -0.005
            brevity_delta = -0.03 if len(content) > 180 else 0.02

            self.memory.record_shared_context(
                server_id=server_id,
                topics=topics,
                participants=participants,
                weight=stimulus.salience,
            )
            self.memory.adjust_user_profile(
                server_id=server_id,
                user_id=str(author_id),
                deltas={
                    "verbosity": verbosity_delta,
                    "humor_tolerance": humor_delta,
                    "tone_balance": tone_delta,
                    "success_rate": success_delta,
                    "warmth": warmth_delta,
                    "formality": formality_delta,
                    "precision": precision_delta,
                    "brevity_bias": brevity_delta,
                },
            )
            for other in mentioned_ids:
                self.memory.update_relationship(
                    server_id=server_id,
                    user_a=str(author_id),
                    user_b=other,
                    deltas={
                        "topic_overlap": 0.04 * len(topics),
                        "affinity": 0.02 * outcome_score,
                    },
                )
            # Track Vyxen ↔ user relationship as well
            self.memory.update_relationship(
                server_id=server_id,
                user_a=str(author_id),
                user_b="vyxen",
                deltas={"trust": 0.03 * outcome_score, "affinity": 0.02},
            )

            self._capture_important_memory(server_id, str(author_id), content)

        # Capture trimmed content to avoid raw logging and recursive growth.
        context_snippet: Dict[str, Any] = {}
        for key, value in stimulus.context.items():
            if key in {"content", "memory_echoes"}:
                continue
            if key == "attachments" and isinstance(value, list):
                context_snippet["attachment_count"] = len(value)
                continue
            if key in {"mentioned_user_ids", "channel_mentions", "role_mentions"} and isinstance(value, list):
                context_snippet[f"{key}_count"] = len(value)
                continue
            context_snippet[key] = value
        content_full = stimulus.context.get("content")
        if isinstance(content_full, str) and content_full:
            context_snippet["content_len"] = len(content_full)
            try:
                context_snippet["topics"] = extract_topics(content_full)
            except Exception:
                pass
            context_snippet["has_question"] = "?" in content_full

        mem_stimulus = Stimulus(
            type=stimulus.type,
            source=stimulus.source,
            context=context_snippet,
            salience=stimulus.salience,
            routing=stimulus.routing,
            timestamp=stimulus.timestamp,
        )

        self.memory.record(
            server_id=stimulus.context.get("server_id", "global"),
            stimulus=mem_stimulus,
            interpretations={o.reality: o.to_dict(include_metadata=False) for o in interpretations},
            decision=decision.rationale,
            action=decision.intent.to_dict(include_metadata=False),
            outcome=result.to_dict(include_metadata=False),
            confidence_delta=confidence_delta,
        )

    def _reflect_session_end(self, session: ConversationSession, reason: str) -> None:
        if self.state.safe_mode and not self.memory.allow_writes:
            return
        duration = max(0.0, session.last_interaction - session.session_start)
        engagement = clamp01(duration / self.config.session_ttl_seconds)
        outcome_score = engagement - (0.1 if reason == "timeout" else 0.0)
        server_id = session.guild_id or "global"

        self.identity.adjust_from_outcome(outcome_score * 0.4)
        self.memory.adjust_user_profile(
            server_id=server_id,
            user_id=str(session.user_id),
            deltas={"success_rate": outcome_score * 0.1, "verbosity": engagement * 0.05},
        )
        self.memory.update_relationship(
            server_id=server_id,
            user_a=str(session.user_id),
            user_b="vyxen",
            deltas={"trust": engagement * 0.08, "affinity": engagement * 0.06},
        )
        self.memory.record_session_summary(
            server_id=server_id,
            session_context={
                "user_id": session.user_id,
                "channel_id": session.channel_id,
                "guild_id": session.guild_id,
                "duration": duration,
                "messages": session.message_count,
                "ended_at": time.time(),
                "reason": reason,
            },
            outcome_score=outcome_score,
            decision=f"session_end:{reason}",
        )

    def _capture_important_memory(self, server_id: str, user_id: str, content: str) -> None:
        lowered = content.lower()
        weight = 0.0
        if any(phrase in lowered for phrase in ["call me", "my name is", "you can call me", "i prefer being called", "i prefer to be called"]):
            name = ""
            if "my name is" in lowered:
                name = content.split("my name is", 1)[-1].strip()
            elif "i prefer being called" in lowered:
                name = content.split("i prefer being called", 1)[-1].strip()
            elif "i prefer to be called" in lowered:
                name = content.split("i prefer to be called", 1)[-1].strip()
            else:
                # Handles both "call me" and "you can call me"
                name = content.split("call me", 1)[-1].strip()
            # Trim common punctuation tails.
            name = name.strip(" .,:;\"'“”")
            if name:
                self.memory.save_important(server_id, user_id, "preferred_name", name[:64], weight=0.8)
                weight += 0.2
        if "favorite car" in lowered:
            import re

            m = re.search(r"\b(?:my\s+)?favorite\s+car\s+is\s+(.+)$", content, flags=re.IGNORECASE)
            if m:
                fav = (m.group(1) or "").strip(" .,:;\"'“”")
                if fav:
                    self.memory.save_important(server_id, user_id, "favorite_car", fav[:96], weight=0.8)
                    weight += 0.2
        if "pronouns" in lowered:
            pronouns = content.split("pronouns")[-1].strip(": ").strip()
            if pronouns:
                self.memory.save_important(server_id, user_id, "pronouns", pronouns[:64], weight=0.8)
                weight += 0.2
        if "i like" in lowered or "i love" in lowered:
            like_part = content.split("i like")[-1] if "i like" in lowered else content.split("i love")[-1]
            like_part = like_part.strip(" .,:;")[:120]
            if like_part:
                self.memory.save_important(server_id, user_id, "likes", like_part, weight=0.6)
                weight += 0.1
        if "i dislike" in lowered or "i don't like" in lowered or "i do not like" in lowered:
            dislike_part = (
                content.split("i dislike")[-1]
                if "i dislike" in lowered
                else content.split("i don't like")[-1]
                if "i don't like" in lowered
                else content.split("i do not like")[-1]
            )
            dislike_part = dislike_part.strip(" .,:;")[:120]
            if dislike_part:
                self.memory.save_important(server_id, user_id, "dislikes", dislike_part, weight=0.6)
                weight += 0.1
        if "don't ping me" in lowered or "do not ping me" in lowered:
            self.memory.save_important(server_id, user_id, "boundaries", "no pings", weight=0.9)
            weight += 0.1
        if "keep it short" in lowered or "short replies" in lowered:
            self.memory.save_important(server_id, user_id, "communication", "prefers concise", weight=0.7)
            weight += 0.05


class GovernorDecisionWrapper:
    def __init__(
        self,
        intent: ActionIntent,
        interpretations: List[RealityOutput],
        confidence: float,
        risk: float,
        rationale: str,
    ):
        self.intent = intent
        self.interpretations = interpretations
        self.confidence = confidence
        self.risk = risk
        self.rationale = rationale
