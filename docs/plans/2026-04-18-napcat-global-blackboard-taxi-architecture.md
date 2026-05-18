# NapCat Global Blackboard Taxi Architecture

Date: 2026-04-18

## Goal

Replace the current NapCat gateway main-path `orchestrator LLM -> background child`
handoff with a direct dispatcher + sticky full-agent worker model backed by a
global blackboard.

The new design should:

- remove the routing-LLM round trip from the main path
- preserve current transcript, queue, cancel, and progress semantics where
  practical
- keep every worker as a full-capability agent
- coordinate live work through a global blackboard instead of prompt-level
  cross-agent context sharing
- minimize merge cost by concentrating the change inside branch-local NapCat
  gateway files and by evolving existing session/task models

## Problems In The Current Design

The current NapCat path has three structural latency and coordination costs:

1. Every orchestrated turn pays an extra routing LLM call before any heavy work
   begins.
2. Background child agents are one-shot tasks, so same-topic follow-ups require
   a controller decision and handoff instead of going back to an already-owned
   worker.
3. Live work state is only partially shared through session-local child state,
   which is enough for status replies but not a complete coordination surface
   for multi-worker routing.

The obvious but wrong alternative is to let every full agent consume all other
running agents' live state inside prompt context. That would reduce handoff
   logic, but it would also:

- destabilize prompt-cache prefixes
- grow token cost with concurrency
- inject stale or irrelevant global state into unrelated turns
- make shared workspace execution harder to reason about

The design here avoids that by moving shared state into a runtime blackboard,
not into every agent's prompt.

## Design Principles

1. Route with deterministic runtime state before using a model.
2. Keep live coordination out of the prompt prefix.
3. Reuse an active worker for same-task follow-ups.
4. Preserve transcript hygiene: visible outcomes only, not private execution
   chatter.
5. Keep branch-local modifications inside NapCat-specific gateway files when
   possible.
6. Evolve `GatewayOrchestratorSession` and `GatewayChildState` instead of
   inventing a second parallel task system.
7. Keep runtime session isolation per worker/task even when work is sticky.

## High-Level Architecture

The new system has four layers:

1. `dispatcher`
   A lightweight non-LLM routing layer inside the NapCat gateway extension.
2. `global_blackboard`
   The in-memory shared runtime state for all active and recent work.
3. `sticky_workers`
   Full-capability agents that own tasks and publish progress to the
   blackboard.
4. `session_view`
   Session-local views derived from the blackboard for transcript logic, status
   replies, and queue UX.

The main flow becomes:

1. `gateway/run.py` still delegates the turn through the runtime extension.
2. `NapCatGatewayExtension.run_orchestrator_turn(...)` becomes a dispatcher
   entrypoint, not a routing-LLM wrapper.
3. The dispatcher checks group relevance, active tasks, dedupe matches, queue
   capacity, and worker ownership.
4. It either:
   - returns an immediate status or ignore response
   - reuses an existing active worker
   - starts a new worker
   - queues a new task and returns a handoff reply
5. Workers execute through the existing full agent path and continuously update
   the blackboard.
6. Final user-visible worker replies are appended back into the main transcript
   exactly once.

## Core Runtime Model

### Blackboard

Add a branch-local global blackboard owned by the NapCat gateway extension.

The blackboard is:

- process-local
- in-memory
- authoritative for live task coordination
- not injected wholesale into worker prompts

Suggested task record shape:

```python
@dataclass
class BlackboardTask:
    task_id: str
    session_key: str
    parent_session_id: str
    owner_worker_id: str

    goal: str
    originating_user_message: str
    dedupe_key: str
    request_kind: str = "analysis"
    priority: str = "normal"

    status: str = "queued"  # queued, running, completed, failed, cancelled
    current_step: str = ""
    current_tool: str = ""
    latest_progress: str = ""
    tool_history: list[dict[str, Any]] = field(default_factory=list)

    summary: str = ""
    public_reply: str = ""
    artifacts: list[str] = field(default_factory=list)

    runtime_session_id: str = ""
    runtime_session_key: str = ""
    cancel_requested: bool = False

    started_at: str = ""
    finished_at: str = ""
    updated_at: str = ""
```

Required blackboard indexes:

- by `task_id`
- by `session_key`
- by `owner_worker_id`
- by session-scoped `dedupe_key`
- a queue ordering surface for queued tasks

### Session State

Keep `GatewayOrchestratorSession` as the session-local control-plane object, but
change its role:

- it becomes a session view and queue facade over blackboard state
- `active_children` and `recent_children` remain available as compatibility
  views, backed by blackboard task state
- `routing_history` becomes dispatcher decision history instead of
  orchestrator-model output history
- `last_decision_summary` remains useful for diagnostics and observability

### Worker State

Keep `GatewayChildState` as the per-task runtime state object, but reinterpret
it as a sticky worker-owned task record rather than a one-shot child handoff.

Important invariants to preserve:

- each running task keeps an isolated `runtime_session_id`
- each running task keeps an isolated `runtime_session_key`
- finalization still archives active work into recent work
- queue drain still happens after terminal lifecycle events

## Dispatcher Behavior

`NapCatGatewayExtension.run_orchestrator_turn(...)` should become the direct
dispatcher entrypoint.

The dispatcher should not call the routing LLM in normal operation.

Evaluation order:

1. Normalize session, turn metadata, and group trigger context.
2. If the message is ignorable low-value group chatter, ignore immediately.
3. If the message is a status query for active work, answer from blackboard.
4. If the message dedupes to an active task in the same session, reuse that
   task and return current status or a short acknowledgement.
5. If the message is a same-topic follow-up for a queued task, keep it queued
   under the same owner and return queue status.
6. If the message is new work and capacity is available, allocate a worker and
   start immediately.
7. If the message is new work and capacity is full, enqueue and return a queue
   or handoff reply.

The dispatcher is allowed to emit immediate user-facing text for:

- status replies
- reuse acknowledgements
- queue acknowledgements
- ignore decisions that map to no visible response

The dispatcher should not generate or store orchestrator JSON.

## Sticky Worker Model

Every taxi is a full-capability agent worker.

Worker rules:

- workers execute through the existing `_run_agent` path
- workers remain tool-capable and use the same runtime safety constraints as
  current child agents
- workers publish progress through existing progress/runtime callbacks into the
  blackboard
- follow-up turns for the same dedupe key reuse the same active worker when one
  exists
- new distinct work creates a new task record and either starts or queues based
  on capacity

Sticky assignment rule:

- the ownership key is `session_key + dedupe_key`
- while a task is `queued` or `running`, same-task follow-ups route back to
  that task
- after terminal completion, a fresh request may create a new task unless
  product logic explicitly treats it as a status follow-up on recent work

## Worker Runtime Policy

The current orchestrator-generated `complexity_score` is removed as the primary
main-path routing primitive.

Worker model/runtime resolution should become deterministic:

- if the turn already carries a promotion plan, honor it
- otherwise use the session's resolved runtime
- same-task follow-ups reuse the already-selected runtime for the active task

This change has an important interaction with NapCat's existing automatic
high-tier model switching:

- the current orchestrator path uses orchestrator-generated
  `complexity_score` as one of the inputs for child L1/L2 selection
- a direct dispatcher + sticky worker design removes that score as a normal-path
  routing signal
- without replacement logic, NapCat's current child-side auto-promotion would
  regress

The replacement rule is:

- explicit promotion plans from `prepare_gateway_turn()` remain authoritative
- sticky reuse is the default, but it must allow one-way upgrade from a lower
  tier worker to a higher tier worker
- task reuse must never pin a task forever on an underpowered worker when the
  new turn clearly requires a stronger model

### Promotion Compatibility Rules

The dispatcher must keep consuming `NapCatPromotionPlan` from the turn plan.

New task creation:

- if the turn plan forces direct L2, create the task on an L2 worker
- if the turn plan selects L1, create the task on an L1 worker
- if promotion is inactive, use the normal session runtime resolution

Active task follow-up:

- reuse the active worker by default
- if the new turn's promotion plan requires a stronger tier than the current
  worker, request a one-way upgrade
- do not downgrade an active L2 worker back to L1

Upgrade trigger sources:

- explicit L2 promotion plan or explicit stronger-model user request
- dispatcher heuristics for clearly high-complexity requests
- blackboard/runtime execution signals that already imply the current worker is
  underpowered

Execution-signal triggers should mirror the existing promotion safeguards where
practical:

- too many API calls
- too many tool calls
- use of force-L2 toolsets
- repeated same-task follow-ups with no terminal outcome

### Upgrade Semantics

Do not hot-swap the model inside a running worker.

When an upgrade is required:

- mark the task as `upgrade_requested`
- stop routing new user turns to the old worker
- create a successor worker on the stronger tier
- carry forward only the task summary, recent transcript slice, and relevant
  blackboard task state
- preserve transcript continuity by keeping the same user-visible session and
  task identity

This keeps runtime/session isolation intact and makes upgrade behavior explicit
for observability and tests.

Tool policy:

- keep `child_default_toolsets` as the default worker toolset policy surface
- keep dynamic disabled toolsets enforced
- do not add any prompt-level "other workers are running" context
- if a worker needs coordination information, fetch a small task-scoped summary
  from runtime state rather than injecting the full blackboard

## Transcript And User-Visible Behavior

Keep current transcript hygiene wherever possible.

Write back:

- the user turn that created or updated visible work
- direct dispatcher replies when they are user-visible
- worker public final replies
- worker public error replies

Do not write back:

- raw blackboard state
- worker tool chatter
- private reasoning
- internal progress updates unless they were intentionally turned into a
  user-visible status reply

Behavioral expectations:

- status queries still answer quickly and do not create new work
- duplicate requests still reuse active work
- queue acknowledgements remain concise and immediate
- worker final replies still append into the main transcript once

## Migration Strategy

This design is a direct replacement of the current orchestrator LLM main path.

Migration steps:

1. Introduce the global blackboard and blackboard-backed task lifecycle while
   preserving current session/task surfaces.
2. Rework `run_orchestrator_turn(...)` into a dispatcher that uses session +
   blackboard state instead of `run_orchestrator_agent(...)`.
3. Remove normal-path routing dependence on orchestrator JSON parsing.
4. Keep queue drain, cancellation, transcript append, and observability flows
   attached to the evolved task lifecycle.
5. Retain compatibility shims only where required to avoid broad test and call
   site breakage.

The old routing-LLM helper may be deleted once the dispatcher path fully
replaces it and the tests move to the new semantics.

## Files And Boundaries

Primary implementation should stay concentrated in:

- `gateway/napcat_gateway_extension.py`
- `gateway/napcat_orchestrator_runtime.py`
- `gateway/napcat_orchestrator_support.py`
- `gateway/orchestrator.py`

If a new branch-local helper file is needed for the blackboard runtime, place
it under `gateway/` next to the existing NapCat orchestration helpers rather
than modifying unrelated upstream files.

Avoid turning this into a cross-platform gateway abstraction in v1.

## Detailed Implementation Breakdown

### 1. `gateway/orchestrator.py`

Use this file for data-model evolution only. Do not put NapCat-specific routing
logic here.

Required changes:

- add a `BlackboardTask` dataclass or equivalent blackboard-owned task record
- add explicit fields needed for sticky-worker ownership and promotion upgrade
  tracking
- keep `GatewayChildState` as the compatibility-facing runtime/task shape used
  by tests and session views
- add conversion helpers between blackboard task records and
  `GatewayChildState` session views
- keep fuzzy dedupe helpers unchanged unless a concrete blackboard requirement
  forces an adjustment

New fields that must exist somewhere in the runtime model:

- `owner_worker_id`
- `task_id`
- `upgrade_requested`
- `superseded_by_task_id`
- `worker_tier`
- `worker_model_override`
- `task_seq` or equivalent stable queue ordering marker

### 2. New branch-local blackboard helper under `gateway/`

Add one branch-local helper dedicated to blackboard state management. Keep it
NapCat-focused and runtime-only.

Responsibilities:

- own the global in-memory task store
- maintain indexes by task, session, worker, and session-scoped dedupe key
- expose atomic operations for create, update, queue, start, finalize, cancel,
  and upgrade
- expose session-scoped query helpers that the dispatcher can call without
  reconstructing state from multiple locations

Required operations:

- `create_task(...)`
- `get_task(task_id)`
- `find_active_task(session_key, dedupe_key)`
- `list_session_active_tasks(session_key)`
- `list_session_recent_tasks(session_key)`
- `enqueue_task(task_id)`
- `mark_running(task_id, ...)`
- `update_progress(task_id, ...)`
- `mark_terminal(task_id, status, ...)`
- `request_upgrade(task_id, ...)`
- `cancel_tasks(session_key, child_ids=None, ...)`
- `next_queued_task(session_key)`

Concurrency rule:

- all blackboard mutations must happen behind one extension-owned lock or one
  clearly isolated mutation seam; do not spread mutation ordering across
  unrelated host objects

### 3. `gateway/napcat_gateway_extension.py`

This becomes the main orchestration host and should own:

- the blackboard runtime instance
- session view hydration from blackboard state
- dispatcher entrypoint logic in `run_orchestrator_turn(...)`
- session-scoped queue/cancel facade methods

Required edits:

- initialize blackboard state in the extension constructor
- rewrite `get_or_create_orchestrator_session()` so new sessions are lightweight
  facades, not the primary source of active-task truth
- update `iter_orchestrator_sessions()` and related helpers to derive task views
  from blackboard-backed state
- rework `cancel_orchestrator_children()` to cancel through blackboard first,
  then worker runtime controls

Dispatcher contract for `run_orchestrator_turn(...)`:

- input stays unchanged so `gateway/run.py` does not need a new interface
- output remains `Optional[str]`
- it must never require orchestrator JSON parsing in the normal path
- it may still emit the same observability event family, but with dispatcher
  semantics instead of controller-model semantics

### 4. `gateway/napcat_orchestrator_support.py`

This file should stop being the home of orchestrator-model prompt logic and
become the policy/helper layer for dispatcher decisions and worker model
selection.

Keep:

- dedupe logic
- status formatting
- task-board formatting
- promotion policy helpers
- transcript formatting helpers

Remove or demote from normal-path use:

- orchestrator system prompt construction
- orchestrator decision canonicalization as the core routing contract
- normal-path dependence on `parse_orchestrator_decision(...)`

Add or refactor helpers for:

- `classify_dispatch_turn(...)`
- `should_ignore_group_message(...)`
- `resolve_task_reuse(...)`
- `resolve_new_task_policy(...)`
- `resolve_worker_runtime_policy(...)`
- `should_upgrade_worker(...)`
- `build_queue_ack_reply(...)`
- `build_reuse_ack_reply(...)`

### 5. `gateway/napcat_orchestrator_runtime.py`

This file should become the worker lifecycle runtime.

Keep:

- progress callback integration
- runtime callback integration
- task start/finalize mechanics
- queue drain mechanics
- transcript append helpers
- public reply delivery

Required edits:

- remove normal-path reliance on `run_orchestrator_agent(...)`
- rename or conceptually reinterpret child lifecycle helpers as worker/task
  lifecycle helpers where practical
- create worker launch from blackboard task records, not from orchestrator JSON
  child requests
- make `spawn_orchestrator_children(...)` either a compatibility shim over task
  allocation or replace it with a task/worker allocation helper used by the
  dispatcher
- implement successor-worker creation for one-way L1 -> L2 upgrades

Worker launch requirements:

- preserve isolated `session_id` and `session_runtime_key`
- preserve transcript append behavior
- preserve direct public reply capability
- register runtime controls back into blackboard-backed task state

## State Migration And Compatibility Rules

### Session view compatibility

For v1, keep these surfaces valid:

- `GatewayOrchestratorSession.active_children`
- `GatewayOrchestratorSession.recent_children`
- `GatewayOrchestratorSession.child_board()`
- current status formatting helpers

Implementation rule:

- these become derived views refreshed from blackboard state before read access
- tests that inspect these structures should continue to pass after semantic
  updates

### Routing history compatibility

Do not store old orchestrator JSON decisions as the new source of truth.

Instead:

- keep `routing_history` as a compact control-plane log
- write dispatcher decision summaries in a stable compact format
- preserve enough detail for:
  - duplicate detection context
  - recent-decision diagnostics
  - same-topic follow-up floors if still used

### Cancellation compatibility

Cancellation order must be:

1. blackboard task marked `cancel_requested`
2. session view refreshed
3. runtime control interrupted
4. running asyncio task cancelled if needed
5. finalization archived through the same blackboard/task path

Do not allow cancellation to finalize only the compatibility view while leaving
the blackboard active.

### Transcript compatibility

Single-write guarantees must remain:

- one user turn write for spawned/queued work
- one visible final assistant write per worker outcome
- no duplicate writes when a worker already sent a visible reply

The dispatcher may add new concise reuse/queue replies, but they must still
follow the same single-write rule.

## Interface And Policy Decisions

### Dispatcher outcome contract

The dispatcher has four internal outcomes:

- `ignore`
- `reply_status`
- `reuse_task`
- `start_or_queue_task`

Externally it still returns either:

- `None`
- a user-visible reply string

### Worker tiers

Define worker tier as a small internal enum or normalized string:

- `default`
- `l1`
- `l2`

Rules:

- `default` means session/runtime default with no explicit promotion tier
- `l1` and `l2` are attached when promotion policy selected them
- once a task reaches `l2`, it may not transition back to `l1`

### Successor-worker upgrade contract

When an upgrade is triggered:

- old task gets `upgrade_requested=True`
- old task is no longer selected by task-reuse routing for new turns
- new task stores `supersedes_task_id=<old_task_id>`
- old task stores `superseded_by_task_id=<new_task_id>`
- transcript remains attached to the same main session
- blackboard session queries should prefer the latest active successor when
  presenting status

### Queue policy

The queue remains session-scoped for v1.

Required rules:

- `child_max_concurrency` continues to define max concurrently running workers
  per session
- queued tasks preserve insertion order unless explicit future priority policy
  is added
- same-dedupe follow-ups to a queued task do not create a second queued task

## Execution Order

Implement in this order to keep the branch working throughout the refactor:

1. Add blackboard data model and helper with no behavior change yet.
2. Make session views read from blackboard-backed state while preserving
   existing tests for child boards and progress views.
3. Route current child lifecycle writes through blackboard so the old
   orchestrator path and the new state store coexist temporarily.
4. Replace `run_orchestrator_turn(...)` decisioning with the dispatcher.
5. Switch task allocation to dispatcher-created blackboard tasks.
6. Remove normal-path orchestrator-LLM dependence.
7. Add one-way worker upgrade logic for promotion compatibility.
8. Delete or quarantine obsolete orchestrator prompt/decision code once tests
   are green.

The critical sequencing rule is:

- do not replace session/task state and routing logic in the same step without
  first making blackboard the shared source of truth

## Testing Plan

Update the current gateway orchestrator test suite to validate dispatcher and
blackboard behavior instead of orchestrator-model JSON behavior.

Required scenarios:

1. Active task status query returns a direct reply without invoking routing LLM
   logic.
2. Duplicate same-goal request reuses the existing active task.
3. New work starts immediately when session capacity allows.
4. New work queues when session capacity is full and returns a queue or handoff
   reply.
5. Queue drain starts the next queued task after the running task finalizes.
6. Worker progress updates are reflected in session task-board output.
7. Worker final reply appends to the main transcript exactly once.
8. Worker with already-visible assistant output still does not double-append.
9. Cancel active task marks blackboard and session state consistently and
   prevents queued duplicate execution.
10. Low-value unrelated group message is ignored without creating a task.
11. Same-session different goals create distinct running/queued tasks with
    stable ownership.
12. Runtime session ids and runtime session keys remain isolated per worker.
13. Main dispatcher path does not depend on orchestrator JSON parsing in normal
    operation.
14. Explicit stronger-model turn creates a high-tier worker without a routing
    LLM decision.
15. New low-tier task stays on L1 when the promotion plan selects L1.
16. Same-task follow-up requesting stronger analysis upgrades an active L1
    worker to L2 instead of silently reusing L1 forever.
17. Active L2 worker is never downgraded by later lower-tier follow-ups.
18. Force-L2 execution signals from runtime metrics trigger successor-worker
    upgrade when safeguards require it.
19. Session model override still bypasses promotion in the new dispatcher path.

## Acceptance Criteria

The design is complete when:

- the main NapCat path no longer needs a routing LLM round trip
- all live work coordination is derivable from the blackboard
- same-task follow-ups reuse active workers
- prompt construction never includes full global blackboard contents
- NapCat automatic high-tier model switching still works without relying on
  orchestrator-generated `complexity_score`
- same-task reuse supports one-way L1 -> L2 upgrade when a stronger model is
  required
- transcript semantics remain readable and non-duplicative
- existing cancellation, queue drain, and observability expectations continue to
  work on top of the new task model

## Defaults Chosen

- rollout is direct replacement, not config-gated dual-track migration
- the blackboard is in-memory and process-local in v1
- existing `GatewayOrchestratorSession` and `GatewayChildState` are evolved
  instead of replaced wholesale
- current child concurrency, direct-reply, progress-reply, and default-toolset
  config surfaces remain the default worker policy knobs unless later cleanup
  removes them
