# LightRAG Memory Provider

Hermes can use LightRAG as an external memory provider with:

- automatic recall via `/query/data`
- optional explicit tools: `lightrag_search`, `lightrag_answer`
- batched append-only writes via `/documents/text`

## Activation

Set `memory.provider: lightrag` in `config.yaml`.

Minimal config:

- `LIGHTRAG_ENDPOINT` or `$HERMES_HOME/lightrag.json` `endpoint`
- `LIGHTRAG_API_KEY` optional
- `LIGHTRAG_WORKSPACE` optional, otherwise derived as `hermes_<profile>`

Example `lightrag.json`:

```json
{
  "endpoint": "http://127.0.0.1:9621",
  "memory_mode": "hybrid",
  "query_mode": "mix",
  "prefetch_top_k": 5,
  "prefetch_max_chars": 2400,
  "write_batch_turns": 6,
  "write_on_session_end": true,
  "response_type": "Bullet Points"
}
```

## Modes

- `hybrid`: auto recall + tools
- `context`: auto recall only
- `tools`: explicit tools only

## Notes

- Integration is REST-only. Hermes does not embed LightRAG core.
- To make LightRAG the only durable memory backend, set `memory.memory_enabled: false`
  and `memory.user_profile_enabled: false` in `config.yaml`.
- In that mode, Hermes' `memory` tool writes directly to LightRAG-managed explicit
  memory documents instead of `~/.hermes/memories/*`.
- Long-term writes are append-only. Built-in memory `remove` is mirrored as a tombstone note, not a document delete.
- Workspace is sent with the `LIGHTRAG-WORKSPACE` header when configured.
