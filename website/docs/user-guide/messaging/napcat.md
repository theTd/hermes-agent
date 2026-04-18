---
sidebar_position: 5
title: "NapCat / QQ"
description: "Use Hermes through NapCat's OneBot 11 forward WebSocket adapter for QQ private chats and groups"
---

# NapCat / QQ

Hermes supports QQ through a native NapCat gateway adapter over **OneBot 11 forward WebSocket**.

Current scope:

- QQ private chats
- QQ groups
- Native text, image, audio, and file delivery
- Group/user policy controls
- Per-group model routing

## What Makes NapCat Different

NapCat has a few platform-specific behaviors that differ from Telegram or Discord:

- **QQ groups are shared sessions by default**. One group maps to one session unless you explicitly turn `group_sessions_per_user: true` back on.
- **Gateway slash commands are super-admin only** on NapCat. That includes `/model`, `/new`, `/yolo`, and `/sethome`.
- **Streaming and typing indicators are off** because NapCat does not support message editing and the adapter intentionally avoids noisy partial replies.
- **Current-chat history lookup is available** through the `napcat_read_history` tool inside active NapCat sessions.

## Minimal Setup

In NapCat / OneBot 11:

- Enable a **forward WebSocket server**
- Set `messagePostFormat` to `array`

In Hermes:

```env
NAPCAT_WS_URL=ws://127.0.0.1:3001/
NAPCAT_TOKEN=replace-with-real-token
NAPCAT_ALLOWED_USERS=10001
NAPCAT_HOME_CHANNEL=group:123456
```

You can also configure it through:

```bash
hermes gateway setup
```

Then choose `NapCat / QQ`.

## Example `config.yaml`

```yaml
gateway:
  platforms:
    napcat:
      enabled: true
      token: your-onebot-token
      home_channel:
        platform: napcat
        chat_id: group:123456
        name: QQ Home
      extra:
        ws_url: ws://127.0.0.1:3001/
        super_admins:
          - "10001"
        orchestrator:
          enabled_platforms: [napcat]
          child_max_concurrency: 1
          child_default_toolsets: [terminal, file_tools]
          orchestrator_model: minimax-2.7-highspeed
          orchestrator_provider: minimax
          orchestrator_base_url: https://api.minimax.io/anthropic
          orchestrator_reasoning_effort: minimal
        group_trigger_rules:
          "123456":
            require_at: true
        group_policies:
          "123456":
            deny_skills: ["terminal"]
            model_override:
              model: gpt-5.4
              provider: openai-codex
              base_url: https://chatgpt.com/backend-api/codex
              api_key: ${OPENAI_CODEX_API_KEY}
              api_mode: codex_responses
```

NapCat's orchestrator settings should live under
`platforms.napcat.extra.orchestrator`.
Top-level `gateway_orchestrator` is still accepted as a compatibility fallback,
but new NapCat-specific config should use the platform-local location.

## Per-Group Models

NapCat policies support static model routing per group or per user.

Supported keys:

- `model`
- `provider`
- `api_key`
- `base_url`
- `api_mode`

You can place them directly under a policy, or nest them under `model_override`.

Priority order:

1. Current-session `/model`
2. `group_policies.<group_id>`
3. `user_policies.<user_id>`
4. Global default model

That means different QQ groups can use different models without affecting each other.

## Group Session Isolation

NapCat group intake is off by default. Turn it on explicitly if you want Hermes
to process QQ group messages:

```yaml
gateway:
  platforms:
    napcat:
      extra:
        group_chat_enabled: true
```

To allow only specific groups, pass a list of QQ group IDs:

```yaml
gateway:
  platforms:
    napcat:
      extra:
        group_chat_enabled:
          - "547996548"
          - "1098447728"
```

NapCat groups default to:

```yaml
gateway:
  platforms:
    napcat:
      extra:
        group_sessions_per_user: false
```

This is the implicit default even if you omit the key.

If you want one session per user inside the same group, set:

```yaml
gateway:
  platforms:
    napcat:
      extra:
        group_sessions_per_user: true
```

## Sending to NapCat with `send_message`

Explicit targets must include the chat type:

- `napcat:group:123456`
- `napcat:private:10001`

If you use just `napcat`, Hermes sends to the configured NapCat home channel.

Media delivery behavior:

- Images use native `image` segments
- Voice/audio uses native `record` segments
- Other attachments use `file` segments

## History Access Inside a NapCat Session

Active NapCat sessions expose a QQ-specific tool:

- `napcat_read_history`

It automatically binds to the current chat:

- Group chat → `get_group_msg_history`
- Private chat → `get_friend_msg_history`

Typical parameters:

```json
{
  "count": 20,
  "message_seq": 0,
  "reverse_order": false
}
```

## Authorization and Safety

Recommended defaults:

- Start with `NAPCAT_ALLOWED_USERS`
- Keep `require_at: true` in shared groups
- Restrict risky skills with `group_policies` / `user_policies`
- Put only trusted operators in `super_admins`

## More Detail

For the full operational manual, advanced policy examples, and troubleshooting notes, see the repository's `NAPCAT_MANUAL.md`.
