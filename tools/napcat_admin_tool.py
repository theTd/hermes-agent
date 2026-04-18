"""Structured NapCat admin/config management tool.

This tool exists so NapCat / QQ sessions can adjust Hermes settings through a
skill + natural-language workflow without directly editing config.yaml.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

from gateway.config import Platform, load_gateway_config
from gateway.session_context import get_session_env
from hermes_cli.config import get_config_path, save_config
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_QQ_ID_RE = re.compile(r"^\d{5,}$")
_LIST_SETTINGS = {
    "keywords",
    "regexes",
    "allowed_users",
    "denied_users",
    "allow_skills",
    "deny_skills",
}
_TRIGGER_VALUE_SETTINGS = {
    "require_at",
}
_PERSONALITY_KINDS = {
    "default": "default_system_prompt",
    "default_system_prompt": "default_system_prompt",
    "extra": "extra_system_prompt",
    "extra_system_prompt": "extra_system_prompt",
}
_MODEL_OVERRIDE_KEYS = {"model", "provider", "api_mode", "base_url", "api_key"}
_VISIBLE_MODEL_OVERRIDE_KEYS = {"model", "provider", "api_mode"}


NAPCAT_MANAGE_SETTINGS_SCHEMA = {
    "name": "napcat_manage_settings",
    "description": (
        "Inspect and update Hermes NapCat / QQ admin settings for the current "
        "session. Use this inside a NapCat session for tasks like adding or "
        "removing super admins, changing the current group's personality, "
        "toggling group trigger rules, editing current-group allow/deny lists, "
        "or setting the current group's model override. This tool writes structured config "
        "changes and reports that a gateway restart is required for full "
        "effect. Do not use it for API keys, tokens, ws_url, or arbitrary "
        "config editing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "inspect",
                    "list_super_admins",
                    "add_super_admin",
                    "remove_super_admin",
                    "set_group_personality",
                    "clear_group_personality",
                    "set_group_trigger_value",
                    "add_group_list_values",
                    "remove_group_list_values",
                    "replace_group_list_values",
                    "set_group_model_override",
                    "clear_group_model_override",
                ],
                "description": "The NapCat settings operation to perform.",
            },
            "group_id": {
                "type": "string",
                "description": (
                    "Optional QQ group ID. Defaults to the current NapCat group "
                    "when called from a group chat."
                ),
            },
            "target_user_id": {
                "type": "string",
                "description": "Required for add/remove super admin. QQ numeric user ID.",
            },
            "personality_kind": {
                "type": "string",
                "enum": ["default", "extra", "default_system_prompt", "extra_system_prompt"],
                "description": (
                    "Which per-group personality field to modify. "
                    "'default' changes the main per-group personality; "
                    "'extra' adds extra style instructions."
                ),
            },
            "preset_name": {
                "type": "string",
                "description": (
                    "Optional name of a configured agent.personalities preset to "
                    "apply to the current group personality."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Freeform personality text for set_group_personality. "
                    "Ignored if preset_name is provided."
                ),
            },
            "setting": {
                "type": "string",
                "enum": [
                    "require_at",
                    "keywords",
                    "regexes",
                    "allowed_users",
                    "denied_users",
                    "allow_skills",
                    "deny_skills",
                ],
                "description": "Setting name used by trigger/list actions.",
            },
            "value": {
                "description": (
                    "Value for set_group_trigger_value. Use booleans for require_at."
                ),
            },
            "values": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "String values used by add/remove/replace group list actions "
                    "(keywords, regexes, allowed_users, denied_users, allow_skills, deny_skills)."
                ),
            },
            "model": {
                "type": "string",
                "description": "Model name for set_group_model_override.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider slug for set_group_model_override.",
            },
            "api_mode": {
                "type": "string",
                "description": "Optional api_mode override for set_group_model_override.",
            },
        },
        "required": ["action"],
    },
}


def _check_napcat_manage_settings() -> bool:
    if get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower() != "napcat":
        return False

    try:
        config = load_gateway_config()
        pconfig = config.platforms.get(Platform.NAPCAT)
    except Exception:
        return False

    if not pconfig or not getattr(pconfig, "enabled", False):
        return False
    extra = getattr(pconfig, "extra", {}) or {}
    return bool(str(extra.get("ws_url") or "").strip())


def _normalize_qq_id(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not _QQ_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a QQ numeric ID with at least 5 digits.")
    return normalized


def _coerce_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label} must be true or false.")


def _coerce_int(value: Any, *, label: str, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    return parsed


def _normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = [str(value)]

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def _normalize_list_values(setting: str, values: Any) -> list[str]:
    items = _normalize_str_list(values)
    if setting in {"allowed_users", "denied_users"}:
        return [_normalize_qq_id(item, label=setting) for item in items]
    return items


def _resolve_session_context() -> dict[str, str]:
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform != "napcat":
        raise ValueError("napcat_manage_settings is only available inside a NapCat session.")

    return {
        "platform": platform,
        "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip().lower(),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
        "user_name": get_session_env("HERMES_SESSION_USER_NAME", "").strip(),
    }


def _ensure_napcat_config_block(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
        config["platforms"] = platforms

    napcat = platforms.get("napcat")
    if not isinstance(napcat, dict):
        napcat = {}
        platforms["napcat"] = napcat

    extra = napcat.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        napcat["extra"] = extra

    return config, napcat, extra


def _get_group_entry(extra: dict[str, Any], key: str, group_id: str, *, create: bool = False) -> dict[str, Any]:
    container = extra.get(key)
    if not isinstance(container, dict):
        if not create:
            return {}
        container = {}
        extra[key] = container

    entry = container.get(group_id)
    if not isinstance(entry, dict):
        if not create:
            return {}
        entry = {}
        container[group_id] = entry
    return entry


def _cleanup_group_entry(extra: dict[str, Any], key: str, group_id: str) -> None:
    container = extra.get(key)
    if not isinstance(container, dict):
        return
    entry = container.get(group_id)
    if isinstance(entry, dict) and not entry:
        container.pop(group_id, None)
    if not container:
        extra.pop(key, None)


def _resolve_personality_prompt(value: Any) -> str:
    if isinstance(value, dict):
        parts = [str(value.get("system_prompt") or "").strip()]
        tone = str(value.get("tone") or "").strip()
        style = str(value.get("style") or "").strip()
        if tone:
            parts.append(f"Tone: {tone}")
        if style:
            parts.append(f"Style: {style}")
        return "\n".join(part for part in parts if part)
    return str(value or "").strip()


def _available_personality_presets(config: dict[str, Any]) -> list[str]:
    personalities = ((config.get("agent") or {}).get("personalities") or {})
    if not isinstance(personalities, dict):
        return []
    return sorted(str(name).strip() for name in personalities.keys() if str(name).strip())


def _resolve_personality_preset(config: dict[str, Any], preset_name: str) -> str:
    personalities = ((config.get("agent") or {}).get("personalities") or {})
    if not isinstance(personalities, dict):
        raise ValueError("No agent.personalities presets are configured.")

    normalized = str(preset_name or "").strip()
    if not normalized:
        raise ValueError("preset_name is required when applying a personality preset.")

    if normalized not in personalities:
        available = ", ".join(sorted(personalities.keys()))
        raise ValueError(f"Unknown personality preset '{normalized}'. Available: {available}")

    prompt = _resolve_personality_prompt(personalities[normalized])
    if not prompt:
        raise ValueError(f"Personality preset '{normalized}' has no usable prompt.")
    return prompt


def _resolve_target_group_id(args: dict[str, Any], session: dict[str, str]) -> str:
    explicit = str(args.get("group_id") or "").strip()
    if explicit:
        return _normalize_qq_id(explicit, label="group_id")
    if session["chat_type"] == "group" and session["chat_id"]:
        return _normalize_qq_id(session["chat_id"], label="group_id")
    raise ValueError("group_id is required outside a NapCat group chat.")


def _current_super_admins(extra: dict[str, Any]) -> list[str]:
    admins = _normalize_str_list(extra.get("super_admins"))
    normalized: list[str] = []
    for admin in admins:
        try:
            normalized.append(_normalize_qq_id(admin, label="super_admins entry"))
        except ValueError:
            logger.warning("Ignoring invalid NapCat super_admins entry: %r", admin)
    seen: set[str] = set()
    deduped: list[str] = []
    for admin in normalized:
        if admin in seen:
            continue
        deduped.append(admin)
        seen.add(admin)
    return deduped


def _require_super_admin(extra: dict[str, Any], session_user_id: str) -> list[str]:
    admins = _current_super_admins(extra)
    if session_user_id not in admins:
        raise PermissionError("Only NapCat super admins can change NapCat settings.")
    return admins


def _write_config(config: dict[str, Any]) -> None:
    save_config(config)


def _load_raw_config() -> dict[str, Any]:
    config_path = get_config_path()
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded if isinstance(loaded, dict) else {}


def _sanitize_model_override(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in _VISIBLE_MODEL_OVERRIDE_KEYS
        if value.get(key) not in (None, "")
    }


def _sanitize_group_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}

    sanitized: dict[str, Any] = {}
    for key in ("default_system_prompt", "extra_system_prompt"):
        value = str(policy.get(key) or "").strip()
        if value:
            sanitized[key] = value

    for key in ("allow_skills", "deny_skills", "allowed_skills", "denied_skills"):
        values = _normalize_str_list(policy.get(key))
        if values:
            sanitized[key] = values

    nested_override = _sanitize_model_override(policy.get("model_override"))
    if nested_override:
        sanitized["model_override"] = nested_override

    top_level_override = {
        key: policy.get(key)
        for key in _VISIBLE_MODEL_OVERRIDE_KEYS
        if policy.get(key) not in (None, "")
    }
    if top_level_override:
        sanitized["model_override"] = {
            **sanitized.get("model_override", {}),
            **top_level_override,
        }

    return sanitized


def _mutation_result(
    *,
    action: str,
    changed: bool,
    message: str,
    changed_paths: list[str],
    data: dict[str, Any] | None = None,
) -> str:
    payload = {
        "success": True,
        "action": action,
        "changed": changed,
        "message": message,
        "changed_paths": changed_paths,
        "config_path": str(get_config_path()),
        "restart_required": changed,
        "effective_after": "gateway restart" if changed else "",
    }
    if data:
        payload.update(data)
    return tool_result(payload)


async def _handle_napcat_manage_settings(args: dict[str, Any], **_kwargs) -> str:
    try:
        load_gateway_config()
        session = _resolve_session_context()
        config = _load_raw_config()
        config, _napcat_cfg, extra = _ensure_napcat_config_block(config)
        action = str(args.get("action") or "").strip()
        if not action:
            return tool_error("action is required.", success=False)

        if action == "inspect":
            target_group_id = None
            if session["chat_type"] == "group" and session["chat_id"]:
                target_group_id = session["chat_id"]
            elif str(args.get("group_id") or "").strip():
                target_group_id = _resolve_target_group_id(args, session)

            current_group_policy = (
                _get_group_entry(extra, "group_policies", target_group_id, create=False)
                if target_group_id
                else {}
            )
            current_group_trigger = (
                _get_group_entry(extra, "group_trigger_rules", target_group_id, create=False)
                if target_group_id
                else {}
            )
            is_admin = session["user_id"] in _current_super_admins(extra)
            payload = {
                "success": True,
                "action": action,
                "platform": "napcat",
                "config_path": str(get_config_path()),
                "current_chat_type": session["chat_type"],
                "current_chat_id": session["chat_id"],
                "current_user_id": session["user_id"],
                "current_user_name": session["user_name"],
                "current_user_is_super_admin": is_admin,
                "available_personality_presets": _available_personality_presets(config),
                "target_group_id": target_group_id or "",
                "current_group_policy": _sanitize_group_policy(current_group_policy),
                "current_group_trigger_rule": current_group_trigger,
                "restart_required_for_mutations": True,
                "manageable_actions": sorted(NAPCAT_MANAGE_SETTINGS_SCHEMA["parameters"]["properties"]["action"]["enum"]),
            }
            if is_admin:
                payload["super_admins"] = _current_super_admins(extra)
            return tool_result(payload)

        if action == "list_super_admins":
            _require_super_admin(extra, session["user_id"])
            return tool_result(
                success=True,
                action=action,
                super_admins=_current_super_admins(extra),
                config_path=str(get_config_path()),
            )

        if action in {"add_super_admin", "remove_super_admin"}:
            admins = _require_super_admin(extra, session["user_id"])
            target_user_id = _normalize_qq_id(args.get("target_user_id"), label="target_user_id")
            changed = False
            if action == "add_super_admin":
                if target_user_id not in admins:
                    admins.append(target_user_id)
                    extra["super_admins"] = admins
                    _write_config(config)
                    changed = True
                return _mutation_result(
                    action=action,
                    changed=changed,
                    message=(
                        f"Added NapCat super admin {target_user_id}."
                        if changed
                        else f"NapCat super admin {target_user_id} was already present."
                    ),
                    changed_paths=["platforms.napcat.extra.super_admins"] if changed else [],
                    data={"super_admins": admins},
                )

            if target_user_id not in admins:
                return _mutation_result(
                    action=action,
                    changed=False,
                    message=f"NapCat super admin {target_user_id} was not present.",
                    changed_paths=[],
                    data={"super_admins": admins},
                )
            if len(admins) == 1:
                return tool_error("Refusing to remove the last NapCat super admin.", success=False)

            admins = [admin for admin in admins if admin != target_user_id]
            extra["super_admins"] = admins
            _write_config(config)
            return _mutation_result(
                action=action,
                changed=True,
                message=f"Removed NapCat super admin {target_user_id}.",
                changed_paths=["platforms.napcat.extra.super_admins"],
                data={"super_admins": admins},
            )

        _require_super_admin(extra, session["user_id"])

        if action in {"set_group_personality", "clear_group_personality"}:
            group_id = _resolve_target_group_id(args, session)
            kind = _PERSONALITY_KINDS.get(str(args.get("personality_kind") or "default").strip().lower())
            if not kind:
                return tool_error("personality_kind must be 'default' or 'extra'.", success=False)

            group_policy = _get_group_entry(extra, "group_policies", group_id, create=True)
            if action == "clear_group_personality":
                existed = kind in group_policy and str(group_policy.get(kind) or "").strip() != ""
                group_policy.pop(kind, None)
                _cleanup_group_entry(extra, "group_policies", group_id)
                if existed:
                    _write_config(config)
                return _mutation_result(
                    action=action,
                    changed=existed,
                    message=(
                        f"Cleared {kind} for NapCat group {group_id}."
                        if existed
                        else f"No {kind} was set for NapCat group {group_id}."
                    ),
                    changed_paths=[f"platforms.napcat.extra.group_policies.{group_id}.{kind}"] if existed else [],
                    data={"group_id": group_id},
                )

            preset_name = str(args.get("preset_name") or "").strip()
            if preset_name:
                prompt = _resolve_personality_preset(config, preset_name)
            else:
                prompt = str(args.get("text") or "").strip()
                if not prompt:
                    return tool_error("Provide text or preset_name for set_group_personality.", success=False)
            changed = str(group_policy.get(kind) or "") != prompt
            group_policy[kind] = prompt
            if changed:
                _write_config(config)
            return _mutation_result(
                action=action,
                changed=changed,
                message=f"Updated {kind} for NapCat group {group_id}.",
                changed_paths=[f"platforms.napcat.extra.group_policies.{group_id}.{kind}"] if changed else [],
                data={"group_id": group_id, "personality_kind": kind, "prompt": prompt},
            )

        if action == "set_group_trigger_value":
            group_id = _resolve_target_group_id(args, session)
            setting = str(args.get("setting") or "").strip()
            if setting not in _TRIGGER_VALUE_SETTINGS:
                return tool_error(
                    f"setting must be one of: {', '.join(sorted(_TRIGGER_VALUE_SETTINGS))}",
                    success=False,
                )

            group_rule = _get_group_entry(extra, "group_trigger_rules", group_id, create=True)
            raw_value = args.get("value")
            normalized_value = _coerce_bool(raw_value, label=setting)
            changed = group_rule.get(setting) != normalized_value
            group_rule[setting] = normalized_value
            if changed:
                _write_config(config)
            return _mutation_result(
                action=action,
                changed=changed,
                message=f"Updated {setting} for NapCat group {group_id}.",
                changed_paths=[f"platforms.napcat.extra.group_trigger_rules.{group_id}.{setting}"] if changed else [],
                data={"group_id": group_id, "setting": setting, "value": normalized_value},
            )

        if action in {"add_group_list_values", "remove_group_list_values", "replace_group_list_values"}:
            group_id = _resolve_target_group_id(args, session)
            setting = str(args.get("setting") or "").strip()
            if setting not in _LIST_SETTINGS:
                return tool_error(
                    f"setting must be one of: {', '.join(sorted(_LIST_SETTINGS))}",
                    success=False,
                )

            values = _normalize_list_values(setting, args.get("values"))
            if not values and action != "replace_group_list_values":
                return tool_error("values must contain at least one item.", success=False)

            container_key = "group_trigger_rules" if setting in {"keywords", "regexes", "allowed_users", "denied_users"} else "group_policies"
            group_entry = _get_group_entry(extra, container_key, group_id, create=True)
            current_values = _normalize_list_values(setting, group_entry.get(setting))
            updated_values = list(current_values)

            if action == "add_group_list_values":
                for value in values:
                    if value not in updated_values:
                        updated_values.append(value)
            elif action == "remove_group_list_values":
                updated_values = [value for value in updated_values if value not in set(values)]
            else:
                updated_values = values

            changed = updated_values != current_values
            if updated_values:
                group_entry[setting] = updated_values
            else:
                group_entry.pop(setting, None)
                _cleanup_group_entry(extra, container_key, group_id)

            if changed:
                _write_config(config)
            return _mutation_result(
                action=action,
                changed=changed,
                message=f"Updated {setting} for NapCat group {group_id}.",
                changed_paths=[f"platforms.napcat.extra.{container_key}.{group_id}.{setting}"] if changed else [],
                data={"group_id": group_id, "setting": setting, "values": updated_values},
            )

        if action in {"set_group_model_override", "clear_group_model_override"}:
            group_id = _resolve_target_group_id(args, session)
            group_policy = _get_group_entry(extra, "group_policies", group_id, create=True)
            if action == "clear_group_model_override":
                existed = False
                for key in _MODEL_OVERRIDE_KEYS:
                    if key in group_policy:
                        existed = True
                        group_policy.pop(key, None)
                nested = group_policy.get("model_override")
                if isinstance(nested, dict) and nested:
                    existed = True
                group_policy.pop("model_override", None)
                _cleanup_group_entry(extra, "group_policies", group_id)
                if existed:
                    _write_config(config)
                return _mutation_result(
                    action=action,
                    changed=existed,
                    message=(
                        f"Cleared model override for NapCat group {group_id}."
                        if existed
                        else f"No model override was set for NapCat group {group_id}."
                    ),
                    changed_paths=[f"platforms.napcat.extra.group_policies.{group_id}.model_override"] if existed else [],
                    data={"group_id": group_id},
                )

            model = str(args.get("model") or "").strip()
            if not model:
                return tool_error("model is required for set_group_model_override.", success=False)
            override = {"model": model}
            provider = str(args.get("provider") or "").strip()
            api_mode = str(args.get("api_mode") or "").strip()
            if provider:
                override["provider"] = provider
            if api_mode:
                override["api_mode"] = api_mode

            current_override = group_policy.get("model_override")
            if not isinstance(current_override, dict):
                current_override = {}
            changed = current_override != override
            group_policy["model_override"] = override
            if changed:
                _write_config(config)
            return _mutation_result(
                action=action,
                changed=changed,
                message=f"Updated model override for NapCat group {group_id}.",
                changed_paths=[f"platforms.napcat.extra.group_policies.{group_id}.model_override"] if changed else [],
                data={"group_id": group_id, "model_override": override},
            )

        return tool_error(f"Unknown action '{action}'.", success=False)
    except PermissionError as exc:
        return tool_error(str(exc), success=False, code="permission_denied")
    except ValueError as exc:
        return tool_error(str(exc), success=False, code="invalid_input")
    except Exception as exc:
        logger.exception("napcat_manage_settings failed: %s", exc)
        return tool_error(f"NapCat settings update failed: {exc}", success=False)


registry.register(
    name="napcat_manage_settings",
    toolset="hermes-napcat",
    schema=NAPCAT_MANAGE_SETTINGS_SCHEMA,
    handler=_handle_napcat_manage_settings,
    check_fn=_check_napcat_manage_settings,
    is_async=True,
    emoji="🛠️",
    max_result_size_chars=60_000,
)
