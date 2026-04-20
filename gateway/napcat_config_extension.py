"""Branch-local gateway config hooks for NapCat defaults and env overrides."""

from __future__ import annotations

import os

from gateway.config import (
    GatewayOrchestratorConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    get_gateway_orchestrator_config,
)


class NapCatGatewayConfigExtension:
    """Keep NapCat-specific config behavior out of the shared config loader."""

    def apply_platform_defaults(self, config) -> None:
        napcat_cfg = config.platforms.get(Platform.NAPCAT)
        if napcat_cfg is None:
            return

        extra = napcat_cfg.extra
        if not isinstance(extra, dict):
            extra = {}
            napcat_cfg.extra = extra

        extra.setdefault("group_sessions_per_user", False)
        extra.setdefault("group_chat_enabled", False)
        extra.setdefault("orchestrator", {})
        if isinstance(extra["orchestrator"], GatewayOrchestratorConfig):
            extra["orchestrator"] = extra["orchestrator"].to_dict()
        if not isinstance(extra["orchestrator"], dict):
            extra["orchestrator"] = {}

        top_level_orchestrator = get_gateway_orchestrator_config(config).to_dict()
        if not top_level_orchestrator.get("enabled_platforms"):
            # Preserve the NapCat default enablement when the shared fallback
            # layer omitted enabled_platforms and only contributed model params.
            top_level_orchestrator.pop("enabled_platforms", None)

        extra["orchestrator"] = GatewayOrchestratorConfig.from_dict(
            {
                "enabled_platforms": ["napcat"],
                "orchestrator_model": "doubao-seed-2-0-mini-260215",
                "orchestrator_provider": "custom",
                "orchestrator_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "orchestrator_reasoning_effort": "low",
                **top_level_orchestrator,
                "orchestrator_context_length": 32000,
                **extra["orchestrator"],
            }
        ).to_dict()

    def apply_env_overrides(self, config) -> None:
        napcat_ws_url = os.getenv("NAPCAT_WS_URL")
        napcat_token = os.getenv("NAPCAT_TOKEN")
        if napcat_ws_url:
            if Platform.NAPCAT not in config.platforms:
                config.platforms[Platform.NAPCAT] = PlatformConfig()
            config.platforms[Platform.NAPCAT].enabled = True
            config.platforms[Platform.NAPCAT].extra["ws_url"] = napcat_ws_url
            if napcat_token is not None:
                config.platforms[Platform.NAPCAT].token = napcat_token

        napcat_home = os.getenv("NAPCAT_HOME_CHANNEL")
        if napcat_home and Platform.NAPCAT in config.platforms:
            config.platforms[Platform.NAPCAT].home_channel = HomeChannel(
                platform=Platform.NAPCAT,
                chat_id=napcat_home,
                name=os.getenv("NAPCAT_HOME_CHANNEL_NAME", "Home"),
            )

    def bridge_platform_config(self, platform, platform_cfg, bridged: dict[str, object]) -> None:
        if platform != Platform.NAPCAT or not isinstance(platform_cfg, dict):
            return
        if "orchestrator" in platform_cfg:
            bridged["orchestrator"] = platform_cfg["orchestrator"]

    def platform_connection_ready(self, platform, platform_config) -> bool | None:
        if platform != Platform.NAPCAT:
            return None
        extra = getattr(platform_config, "extra", {}) or {}
        return bool(extra.get("ws_url"))
