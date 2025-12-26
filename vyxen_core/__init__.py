"""
Vyxen Core runtime package.

This package contains the cognition loop, stimulus model, memories, identity
tracking, and governor logic that operate independently from any specific I/O
adapter. Discord integration lives in the adapter layer.
"""

from .config import RuntimeConfig
from .stimuli import Stimulus
from .state import InternalState
from .identity import IdentityCore
from .memory import CausalMemory
from .actions import ActionIntent, ActionResult
from .conversation import ConversationSession, SessionStore
from .governor import Governor
from .cognition import CognitionLoop
from .tool_intents import ParsedIntent, parse_natural_language_intent

__all__ = [
    "RuntimeConfig",
    "Stimulus",
    "InternalState",
    "IdentityCore",
    "CausalMemory",
    "ConversationSession",
    "SessionStore",
    "ParsedIntent",
    "parse_natural_language_intent",
    "ActionIntent",
    "ActionResult",
    "Governor",
    "CognitionLoop",
]
