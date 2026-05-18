---
name: hermes-agent-napcat-admin
description: Manage Hermes NapCat / QQ admin and per-group behavior through natural language. Use this in active NapCat sessions to inspect or change super admins, current-group personality, trigger rules, image generation policy, skill allow/deny lists, and per-group model overrides without editing config.yaml manually.
version: 1.0.0
author: Hermes Agent + Codex
license: MIT
metadata:
  hermes:
    tags: [hermes, napcat, qq, admin, settings, gateway, configuration]
    requires_toolsets: [napcat]
    related_skills: [hermes-agent]
---

# Hermes Agent NapCat Admin

Use this skill when the user wants to manage Hermes NapCat / QQ behavior in natural language instead of slash commands.

Primary rule: use the structured `napcat_manage_settings` tool for all supported changes. Do not directly edit `config.yaml` unless the user explicitly asks for manual file editing and the tool cannot represent the request.

## What This Skill Handles

- Inspect the current NapCat session's manageable settings
- List, add, or remove NapCat super admins
- Set or clear the current group's personality
- Adjust current-group trigger values such as `require_at` or LLM gate knobs
- Add, remove, or replace current-group list settings such as `keywords`, `regexes`, `allowed_users`, `denied_users`, `allow_skills`, and `deny_skills`
- Set the current group's image generation policy
- Set or clear the current group's model override

## Workflow

1. If the target group, target user, or target setting is ambiguous, ask one concise clarification question.
2. If the request scope is unclear, call `napcat_manage_settings` with `action="inspect"` first.
3. Translate the user's natural-language request into the smallest valid structured tool call.
4. After a successful mutation, tell the user exactly what changed and explicitly say that a gateway restart is required for full effect.

## Boundaries

- This skill only makes sense inside an active NapCat session.
- Do not use this skill to edit secrets or connection settings such as API keys, tokens, `ws_url`, or arbitrary endpoint credentials.
- Do not promise hot reload. Treat mutations as config changes that require a gateway restart for full effect.
- Prefer current-group changes by default when the conversation is in a group chat, unless the user explicitly names another group.
