"""Napcat-specific adapter type definitions.

These dataclasses were extracted from base.py to avoid merge conflicts with
upstream changes in the same file region.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class AdapterInboundDecision:
    """Platform-specific inbound authorization / routing decision."""
    drop: bool = False
    consume: bool = False
    response: Optional[str] = None
    reason: str = ""
    context_only: bool = False
    bypass_auth: bool = False


@dataclass
class AdapterSessionDefaults:
    """Platform-specific per-session defaults applied by the gateway runner."""
    auto_yolo_default: Optional[bool] = None


@dataclass
class AdapterTurnPlan:
    """Platform-specific turn customizations injected by the gateway runner."""
    extra_prompt: str = ""
    # Gateway routing-layer instructions that may be consumed by a branch-local
    # controller before the full agent turn starts.
    routing_prompt: str = ""
    user_context: str = ""
    # Turn-scoped controller/persona context for gateway-level routers.
    controller_context: str = ""
    message_prefix: str = ""
    direct_response: str = ""
    dynamic_disabled_skills: List[str] = field(default_factory=list)
    dynamic_disabled_toolsets: List[str] = field(default_factory=list)
    super_admin: bool = False

    def __post_init__(self) -> None:
        self.controller_context = str(self.controller_context or "").strip()

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
