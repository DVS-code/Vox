import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Sequence, Tuple


try:  # pragma: no cover - exercised implicitly in runtime
    import discord  # type: ignore
except Exception:  # pragma: no cover
    discord = None


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> Tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN_RE.findall(text or ""))


_FALLBACK_VALID_FLAGS: frozenset[str] = frozenset(
    {
        "add_reactions",
        "administrator",
        "attach_files",
        "ban_members",
        "change_nickname",
        "connect",
        "create_events",
        "create_expressions",
        "create_instant_invite",
        "create_polls",
        "create_private_threads",
        "create_public_threads",
        "deafen_members",
        "embed_links",
        "external_emojis",
        "external_stickers",
        "kick_members",
        "manage_channels",
        "manage_emojis",
        "manage_emojis_and_stickers",
        "manage_events",
        "manage_expressions",
        "manage_guild",
        "manage_messages",
        "manage_nicknames",
        "manage_permissions",
        "manage_roles",
        "manage_threads",
        "manage_webhooks",
        "mention_everyone",
        "moderate_members",
        "move_members",
        "mute_members",
        "priority_speaker",
        "read_message_history",
        "read_messages",
        "request_to_speak",
        "send_messages",
        "send_messages_in_threads",
        "send_polls",
        "send_tts_messages",
        "send_voice_messages",
        "speak",
        "stream",
        "use_application_commands",
        "use_embedded_activities",
        "use_external_apps",
        "use_external_emojis",
        "use_external_sounds",
        "use_external_stickers",
        "use_soundboard",
        "use_voice_activation",
        "view_audit_log",
        "view_channel",
        "view_creator_monetization_analytics",
        "view_guild_insights",
    }
)


def valid_permission_flags() -> frozenset[str]:
    """
    Return all Discord permission flags supported by the installed discord.py.

    Falls back to a pinned list if discord.py isn't importable for some reason.
    """
    try:
        if discord is not None:
            flags = getattr(getattr(discord, "Permissions", None), "VALID_FLAGS", None)
            if flags:
                return frozenset(str(flag) for flag in flags)
    except Exception:
        pass
    return _FALLBACK_VALID_FLAGS


_ALLOW_WORDS: frozenset[str] = frozenset({"allow", "grant", "give", "enable", "permit", "add"})
_DENY_WORDS: frozenset[str] = frozenset({"deny", "disallow", "disable", "block", "prevent", "remove", "revoke", "no"})
_UNSET_WORDS: frozenset[str] = frozenset({"unset", "clear", "reset"})


_CONTEXT_WORDS: frozenset[str] = frozenset(
    {
        "permission",
        "permissions",
        "overwrite",
        "overwrites",
        "allow",
        "grant",
        "give",
        "enable",
        "permit",
        "deny",
        "disallow",
        "disable",
        "block",
        "prevent",
        "remove",
        "revoke",
        "unset",
        "clear",
        "reset",
        "access",
    }
)


_EXTRA_ALIASES: dict[str, Tuple[str, ...]] = {
    # Common user phrasing from Discord UI.
    "view_channel": ("see channel", "see the channel", "access channel", "access the channel"),
    "manage_guild": ("manage server",),
    "use_application_commands": ("use slash commands", "slash commands", "app commands", "application commands"),
    "read_message_history": ("read history", "message history"),
    "moderate_members": ("timeout members", "timeout member"),
    "create_instant_invite": ("create invite", "invite"),
    "view_guild_insights": ("view server insights", "server insights"),
}


@dataclass(frozen=True)
class PermissionParseResult:
    overwrites: Dict[str, Optional[bool]]
    unknown: Tuple[str, ...] = ()


def _singular_variants(tokens: Sequence[str]) -> Iterable[Tuple[str, ...]]:
    """
    Generate a small set of singular/plural variants for matching human phrasing.

    Example: ("ban", "members") -> ("ban", "member")
    """
    if not tokens:
        return []

    plural_indexes: list[int] = []
    for index, token in enumerate(tokens):
        if token.endswith("s") and len(token) > 3:
            plural_indexes.append(index)

    variants: list[Tuple[str, ...]] = [tuple(tokens)]
    for index in plural_indexes:
        variant = list(tokens)
        variant[index] = variant[index].removesuffix("s")
        variants.append(tuple(variant))

    if plural_indexes:
        all_singular = list(tokens)
        for index in plural_indexes:
            all_singular[index] = all_singular[index].removesuffix("s")
        variants.append(tuple(all_singular))

    # De-dupe while preserving order
    seen: set[Tuple[str, ...]] = set()
    ordered: list[Tuple[str, ...]] = []
    for variant in variants:
        if variant in seen:
            continue
        seen.add(variant)
        ordered.append(variant)
    return ordered


def _build_pattern_index(flags: Iterable[str]) -> dict[str, list[Tuple[Tuple[str, ...], str]]]:
    patterns: list[Tuple[Tuple[str, ...], str]] = []
    for flag in set(flags):
        base_tokens = tuple(flag.split("_"))
        for variant in _singular_variants(base_tokens):
            patterns.append((variant, flag))

    for flag, aliases in _EXTRA_ALIASES.items():
        for alias in aliases:
            alias_tokens = _tokenize(alias)
            if not alias_tokens:
                continue
            patterns.append((alias_tokens, flag))

    # Prefer longest matches first
    patterns.sort(key=lambda item: len(item[0]), reverse=True)

    by_first: dict[str, list[Tuple[Tuple[str, ...], str]]] = {}
    for tokens, flag in patterns:
        first = tokens[0]
        by_first.setdefault(first, []).append((tokens, flag))
    return by_first


_VALID_FLAGS = valid_permission_flags()
_PATTERNS_BY_FIRST = _build_pattern_index(_VALID_FLAGS)


def resolve_permission_flag(name: str) -> Optional[str]:
    """
    Resolve a user-provided permission name (snake_case or human phrasing) to a
    discord.py permission flag (e.g. "send messages" -> "send_messages").
    """
    if not name:
        return None

    normalized = re.sub(r"[\s-]+", "_", name.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if normalized in _VALID_FLAGS:
        return normalized

    tokens = _tokenize(name)
    if not tokens:
        return None

    candidates = _PATTERNS_BY_FIRST.get(tokens[0], [])
    for pat_tokens, flag in candidates:
        if len(tokens) == len(pat_tokens) and tuple(tokens) == tuple(pat_tokens):
            return flag
    return None


def parse_permission_overwrites(text: str) -> PermissionParseResult:
    """
    Parse allow/deny/unset permission changes from natural language.

    Returns a mapping suitable for discord.PermissionOverwrite, where values are:
      - True: explicitly allow
      - False: explicitly deny
      - None: clear/unset an overwrite
    """
    if not text:
        return PermissionParseResult(overwrites={})

    overwrites: Dict[str, Optional[bool]] = {}
    unknown: list[str] = []

    # Explicit assignment style: "send_messages=false", "manage roles: true"
    assign_re = re.compile(
        r"(?P<name>[a-zA-Z_][a-zA-Z0-9_ -]{1,60})\s*(?:=|:)\s*(?P<val>true|false|yes|no|on|off|allow|deny|enabled|disabled|unset|clear|reset)",
        re.IGNORECASE,
    )
    for match in assign_re.finditer(text):
        raw_name = (match.group("name") or "").strip()
        raw_val = (match.group("val") or "").strip().lower()
        flag = resolve_permission_flag(raw_name)
        if not flag:
            continue

        if raw_val in {"true", "yes", "on", "allow", "enabled"}:
            overwrites[flag] = True
        elif raw_val in {"false", "no", "off", "deny", "disabled"}:
            overwrites[flag] = False
        elif raw_val in {"unset", "clear", "reset"}:
            overwrites[flag] = None

    tokens = _tokenize(text)
    if not tokens:
        return PermissionParseResult(overwrites=overwrites)

    permission_context = any(tok in _CONTEXT_WORDS for tok in tokens)
    current_value: Optional[bool] = None
    value_explicit = False
    index = 0
    max_tokens = 600

    while index < min(len(tokens), max_tokens):
        token = tokens[index]
        if token in _ALLOW_WORDS:
            permission_context = True
            current_value = True
            value_explicit = True
            index += 1
            continue
        if token in _DENY_WORDS:
            permission_context = True
            current_value = False
            value_explicit = True
            index += 1
            continue
        if token in _UNSET_WORDS:
            permission_context = True
            current_value = None
            value_explicit = True
            index += 1
            continue
        if token in {"permission", "permissions", "overwrite", "overwrites", "access"}:
            permission_context = True
            index += 1
            continue

        if not permission_context:
            index += 1
            continue

        candidates = _PATTERNS_BY_FIRST.get(token, [])
        matched = False
        for pat_tokens, flag in candidates:
            if tokens[index : index + len(pat_tokens)] != tuple(pat_tokens):
                continue
            value = current_value if value_explicit else True
            if flag in overwrites and not value_explicit:
                index += len(pat_tokens)
                matched = True
                break
            overwrites[flag] = value
            index += len(pat_tokens)
            matched = True
            break

        if matched:
            continue

        index += 1

    return PermissionParseResult(overwrites=overwrites, unknown=tuple(unknown))
