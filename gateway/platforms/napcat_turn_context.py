"""NapCat-specific private turn context helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from gateway.platforms.base import AdapterTurnPlan


_NAPCAT_PROMOTION_ATTR = "_napcat_promotion_plan"


@dataclass
class NapCatPromotionPlan:
    """Private per-turn promotion state for the NapCat adapter."""

    enabled: bool = False
    stage: str = ""
    turn_model_override: Dict[str, Any] = field(default_factory=dict)
    protocol_prompt: str = ""
    config_snapshot: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Dict[str, Any] | None) -> "NapCatPromotionPlan":
        payload = dict(data or {})
        return cls(
            enabled=bool(payload.get("enabled", payload.get("promotion_enabled"))),
            stage=str(payload.get("stage") or payload.get("promotion_stage") or "").strip(),
            turn_model_override=dict(payload.get("turn_model_override") or {}),
            protocol_prompt=str(
                payload.get("protocol_prompt")
                or payload.get("promotion_protocol_prompt")
                or ""
            ).strip(),
            config_snapshot=dict(
                payload.get("config_snapshot")
                or payload.get("promotion_config_snapshot")
                or {}
            ),
        )

    def is_active(self) -> bool:
        return bool(self.enabled and self.turn_model_override)


def attach_napcat_promotion_plan(
    turn_plan: AdapterTurnPlan,
    promotion_plan: NapCatPromotionPlan | Dict[str, Any] | None,
) -> AdapterTurnPlan:
    """Attach NapCat-private promotion state to a public adapter turn plan."""

    setattr(turn_plan, _NAPCAT_PROMOTION_ATTR, get_napcat_promotion_plan(promotion_plan))
    return turn_plan


def attach_napcat_turn_metadata(
    turn_plan: AdapterTurnPlan,
    payload: Dict[str, Any] | None,
) -> AdapterTurnPlan:
    """Attach NapCat-private turn metadata from a raw adapter payload."""
    raw = dict(payload or {})
    if any(
        key in raw
        for key in (
            "promotion_enabled",
            "promotion_stage",
            "turn_model_override",
            "promotion_protocol_prompt",
            "promotion_config_snapshot",
        )
    ):
        attach_napcat_promotion_plan(turn_plan, raw)
    return turn_plan


def get_napcat_promotion_plan(value: Any) -> NapCatPromotionPlan:
    """Return the NapCat promotion state for a turn plan or raw payload."""

    if isinstance(value, NapCatPromotionPlan):
        return value
    if isinstance(value, dict):
        return NapCatPromotionPlan.from_mapping(value)
    if isinstance(value, AdapterTurnPlan):
        attached = getattr(value, _NAPCAT_PROMOTION_ATTR, None)
        if isinstance(attached, NapCatPromotionPlan):
            return attached
        if isinstance(attached, dict):
            return NapCatPromotionPlan.from_mapping(attached)
        return NapCatPromotionPlan.from_mapping(
            {
                "promotion_enabled": getattr(value, "promotion_enabled", False),
                "promotion_stage": getattr(value, "promotion_stage", ""),
                "turn_model_override": getattr(value, "turn_model_override", {}),
                "promotion_protocol_prompt": getattr(value, "promotion_protocol_prompt", ""),
                "promotion_config_snapshot": getattr(value, "promotion_config_snapshot", {}),
            }
        )
    return NapCatPromotionPlan()
