# NapCat Gateway Internal Notes

Internal implementation guide for developers and coding agents working on
Hermes' NapCat gateway path.

This file is intentionally different from `NAPCAT_MANUAL.md`:

- `NAPCAT_MANUAL.md` is user-facing setup and operations documentation.
- `NAPCAT.AGENTS.md` is implementation-facing and explains how the current
  NapCat orchestration and observability mechanisms actually work.

## Scope

The current "always orchestrated" NapCat path is the combination of:

- the NapCat adapter receiving and normalizing inbound OneBot events
- the gateway-level session orchestrator deciding `reply` / `spawn` / `ignore`
- background child agents doing heavy work
- NapCat observability stitching the whole chain into a single trace

If you change one of those layers, check the others. Many bugs in this area
come from editing only one stage of a multi-stage pipeline.

## Key Files

- `gateway/platforms/napcat.py`
  - adapter ingress, trigger metadata, root trace creation
- `gateway/napcat_gateway_extension.py`
  - branch-local NapCat host facade and orchestrator turn entry point
- `gateway/napcat_orchestrator_runtime.py`
  - branch-local orchestrator runtime, fallback heuristics, child lifecycle
- `gateway/napcat_orchestrator_support.py`
  - branch-local config/session/transcript helpers for orchestration
- `gateway/run.py`
  - generic gateway runner services consumed by the NapCat extension
- `gateway/orchestrator.py`
  - session state models, dedupe logic, decision parser
- `gateway/napcat_observability/trace.py`
  - trace context object and contextvar propagation
- `gateway/napcat_observability/publisher.py`
  - best-effort event emission queue
- `gateway/napcat_observability/ingest_client.py`
  - queue drain, SQLite persistence, trace aggregation, alert generation
- `gateway/napcat_observability/store.py`
  - SQLite schema for `events`, `traces`, `runtime_state`, `alerts`
- `gateway/napcat_observability/repository.py`
  - filtered query layer for REST/WebSocket consumers
- `hermes_cli/napcat_observability_server.py`
  - FastAPI app, WebSocket bridge, SPA serving
- `run_agent.py`
  - model-call observability and trace-bound agent execution
- `model_tools.py`
  - tool-call observability emitted under the same trace

## Mental Model

For NapCat, a single inbound message now behaves like this:

1. NapCat adapter accepts the raw event and creates a root `NapcatTraceContext`.
2. The adapter emits `message.received` before slow enrichment work.
3. `prepare_gateway_turn()` computes trigger and platform policy context.
4. `GatewayRunner` routes the turn through the session orchestrator.
5. The orchestrator main agent returns structured JSON:
   - `reply`
   - `spawn`
   - `ignore`
6. If the decision spawns work, one or more child agents run in background
   tasks under child spans of the same trace.
7. Public child replies are written back into the main session transcript as
   internal-source assistant messages.
8. Observability events from adapter, orchestrator, model calls, and tools are
   queued, persisted, aggregated into trace summaries, and streamed to the UI.

## Orchestrator Design

### Main controller

The session orchestrator is deliberately lightweight.

- Entry gate: `gateway/run.py` hands NapCat turns to
  `NapCatGatewayExtension.run_orchestrator_turn(...)`
- Per-session state: `GatewayOrchestratorSession`
- Decision type: `OrchestratorDecision`
- Child request type: `OrchestratorChildRequest`

The main orchestrator agent is not the heavy worker.

- It runs with `max_iterations=1`.
- It uses `enabled_toolsets=[]`.
- It uses a fixed system prompt and returns JSON only.
- Dynamic routing context is injected as turn user context, not by mutating the
  core system prompt each turn.

That split matters for both prompt cache stability and correctness.

### Decision contract

Current supported actions are:

- `reply`
- `spawn`
- `ignore`

The parser accepts both the current action-based JSON shape and the older
backward-compatible shape. If parsing fails, the gateway falls back to the
heuristic controller in the branch-local NapCat runtime helpers.

Do not silently extend the controller contract in prompts without updating:

- `gateway/orchestrator.py`
- `gateway/napcat_orchestrator_runtime.py`
- tests in `tests/gateway/test_gateway_orchestrator.py`

### Session memory and routing history

The orchestrator keeps its own compact routing history separate from the full
conversation transcript.

This history is used for:

- duplicate child detection
- follow-up complexity floors
- status replies
- deciding whether a group message is clearly agent-directed

Do not confuse this with the user-visible transcript. The orchestrator history
is a compact control-plane memory, not the source of truth for the full chat.

### Duplicate suppression

Child dedupe is string-based and intentionally fuzzy.

- keys are normalized by `normalize_dedupe_key()`
- similarity uses exact match, substring match for longer strings, and token
  overlap heuristics

If you change dedupe behavior, re-check:

- reused-child status replies
- concurrency limits
- follow-up scoring floors

### Status replies are special

The orchestrator has a dedicated status-reply path.

If a user asks "still there", "any result", "what are you doing", and similar
status queries while a child is active, the main controller should usually
reply from child state instead of spawning anything new.

This path is not just prompt behavior. The branch-local NapCat extension/runtime
also force-overrides the decision in some cases after model output is parsed.

## Child Agent Lifecycle

Background child work is owned by the gateway, not by the generic
`delegate_task` tool.

Each child has:

- a `child_id`
- a child-specific runtime session id and runtime session key
- progress state such as current step, current tool, last activity
- public reply history
- terminal lifecycle event emission

Important behavior:

- child tasks inherit the root `trace_id` via `trace_ctx.child_span(...)`
- child tasks may reply directly to users if `may_reply_directly` is enabled
- child final or error replies are also appended back into the main transcript
- finalized children move from `active_children` to `recent_children`

When changing child execution, check cancellation paths too:

- `/stop`
- session reset
- gateway restart
- orchestrator `cancel_children`

## Transcript Semantics

Main transcript behavior is intentionally asymmetric.

What is written back:

- the user turn that triggered spawned work
- main-agent immediate replies and handoff replies
- child public final replies
- child public error replies

What is not written back in full:

- child internal tool traces
- child full private reasoning transcript
- low-level execution chatter

This keeps the user conversation readable while still preserving visible
outcomes.

## NapCat Observability

### "Observability", not "observatory"

The implemented subsystem name in code and config is `napcat_observability`.
Use that name consistently when editing code, config, docs, or tests.

### Runtime model

NapCat observability currently runs in-process with the gateway.

- startup hook: `gateway/run.py`
- runtime service: `NapcatObservabilityRuntimeService`
- persistence: local SQLite
- query surface: FastAPI
- live stream: WebSocket

If enabled in config, the gateway owns:

- publisher initialization
- ingest thread lifecycle
- optional HTTP monitor port lifecycle

The gateway should continue serving chat traffic even if the observability HTTP
server fails to bind or crashes. That isolation is intentional.

### Trace propagation

The trace object is created at adapter ingress and then threaded through the
rest of the stack with a contextvar.

Root context:

- created in `gateway/platforms/napcat.py`
- includes chat type, chat id, user id, message id

Propagation:

- bound in gateway before agent invocation
- read in `run_agent.py`
- read in `model_tools.py`

Child spans:

- main orchestrator uses `agent_scope="main_orchestrator"`
- child agents use `agent_scope="orchestrator_child"` plus `child_id`

That means a single NapCat message can contain:

- one root inbound trace
- one orchestrator span
- zero or more child spans
- tool and model events emitted under whichever span is currently active

### Event pipeline

Event emission is best-effort.

- callers use `emit_napcat_event(...)`
- events are truncated for large payload fields
- events go into a bounded in-memory queue
- low-priority events may be dropped under pressure
- high-priority events try to evict lower-priority queued events

Do not make correctness depend on observability events always existing.

### What gets observed

Current major event families include:

- adapter lifecycle and inbound message events
- group trigger evaluation and rejection
- gateway turn preparation
- orchestrator turn start / parsed decision / completion / ignore / failure
- child spawn / reuse / cancel / completion / failure
- agent model requested / completed
- reasoning and response deltas
- tool called / completed / failed
- memory and skill usage
- final response, suppression, and raised errors

When adding a new meaningful NapCat-specific behavior, prefer adding a new
event or enriching an existing payload rather than hiding it in logs only.

### Persistence and aggregation

The store is a dedicated SQLite database, separate from normal session
transcript storage.

Tables:

- `events`
- `traces`
- `runtime_state`
- `alerts`

The ingest thread does more than write raw events:

- drains queue batches
- inserts events
- reloads all events for affected traces
- recomputes trace status and summary
- emits alerts for error-severity events

Trace status is derived, not directly authored by runtime code.

Examples:

- active child present -> `in_progress`
- main terminal error or child failure -> `error`
- main completed or child-only completion -> `completed`
- trigger rejected before execution -> `rejected`

If you change terminal event semantics, you must re-check trace aggregation.

### UI surface

The monitor UI consumes:

- REST snapshot/bootstrap queries
- WebSocket `event.append`
- WebSocket `trace.update`
- WebSocket `alert.raised`
- cursor-based backfill after reconnect

The WebSocket layer filters per client by things like:

- `trace_id`
- `session_key`
- `chat_id`
- `user_id`
- `event_type`
- `status`
- `search`

Do not add a new filterable field only in the frontend. Add it through the
repository and WebSocket filtering path too.

## Config Notes

Two config blocks matter here.

### `platforms.napcat.extra.orchestrator`

This is the primary config surface for the NapCat session orchestrator. It
controls:

- enabled platforms
- child max concurrency
- whether children may reply directly
- orchestrator runtime override model/provider/base URL/key
- orchestrator reasoning effort
- child model selection policy

Top-level `gateway_orchestrator` is still read as a compatibility fallback, but
NapCat-specific changes should go under `platforms.napcat.extra.orchestrator`
so the branch-local gateway behavior stays attached to the NapCat adapter
config instead of a global gateway knob.

### `napcat_observability`

This controls monitoring runtime behavior:

- enabled flag
- bind host
- port
- retention days
- queue size
- stdout/stderr truncation limits
- whether control actions are allowed from the UI

## Common Change Hazards

### Hazard 1: editing prompts without editing parser/tests

If you change orchestrator prompt examples or expected JSON shape, update:

- parser
- fallback logic
- orchestrator tests

### Hazard 2: breaking trace continuity

If a new code path forgets to pass the trace context, the UI will show a split
or detached trace. This often happens in new background tasks or helper
threads.

### Hazard 3: writing too much into transcript

Do not dump child internal execution detail into the user transcript. Use
observability for deep internals and transcript only for public conversation.

### Hazard 4: treating observability as authoritative runtime state

The SQLite monitor DB is an analysis surface, not the control-plane source of
truth. Runtime decisions must not depend on querying back the monitor DB.

### Hazard 5: changing terminal events casually

Trace completion state is inferred from terminal event types. Seemingly small
changes in event names or timing can break:

- trace status
- ended-at timestamps
- headline previews
- alert counts
- monitor UI views

## Suggested Debug Order

When NapCat behavior looks wrong, check in this order:

1. adapter ingress and trigger metadata
2. prepared gateway turn payload
3. orchestrator parsed decision
4. child spawn / reuse / skip behavior
5. child progress and terminal events
6. model and tool events under the trace
7. aggregated trace status in `traces`
8. UI bootstrap and WebSocket backfill behavior

That order usually gets you to the real fault faster than starting from the UI.
