# NapCat Single Fullagent Cache Handoff

Date: 2026-04-19

## Goal

The product requirement is now stricter than the current orchestrator design:

- each NapCat session is allowed to have exactly one full-capability background
  agent (`fullagent`)
- the primary engineering goal is stable prompt-cache reuse across consecutive
  fullagent turns

This handoff documents why the current code still misses cross-turn cache in
some cases, what invariants must hold after the fix, and the exact
implementation direction to use.

## Current State

The current branch already includes a partial cache-preservation fix:

- orchestrator child cache entries are no longer destroyed immediately after a
  child finishes
- child runtime identity is currently stabilized by `dedupe_key`
- parent-session reset/model-switch paths evict child cache subtrees

Relevant code:

- `gateway/napcat_orchestrator_runtime.py`
- `gateway/napcat_orchestrator_support.py`
- `gateway/orchestrator.py`
- `gateway/run.py`

This partial fix is not sufficient for the new product requirement, because it
still allows more than one logical fullagent worker per session.

## Trace Evidence

### 1. `c995d477f7fb40ed9897a11a867595b0`

This trace ran a `kst` fullagent child with:

- `runtime_session_id = 20260419_041545_c4441576__orchestrator_worker__fbd36c1d694a5f06`
- `runtime_session_key = agent:main:napcat:group:547996548::child:fbd36c1d694a5f06`

Observed model calls for the child:

- first child model call: `cache_read_tokens = 0`
- second child model call: `cache_read_tokens = 15227`
- third child model call: `cache_read_tokens = 16123`

Interpretation:

- this trace did not reuse cache from a previous fullagent
- it only reused cache from earlier model calls inside the same fullagent run

### 2. `98f0ad484e78496fa508b566acd8ef34`

This later trace reused the same runtime worker:

- `runtime_session_id = 20260419_041545_c4441576__orchestrator_worker__fbd36c1d694a5f06`

Its first child model call already had:

- `cache_read_tokens = 14331`

Interpretation:

- this is the first confirmed cross-trace cache reuse for that worker
- the partial fix works only when the next turn resolves to the exact same
  stable worker identity

### 3. `40d6768f79a14faeace0f1110cc7c858`

This later `kst`-like trace did not reuse the previous worker. It created:

- `runtime_session_id = 20260419_041545_c4441576__orchestrator_worker__5e9871756b8508e7`

Its goal text was:

- `再次执行 kst 指令，调用 pixiv-soft-r15 skill 找图并发送`

Interpretation:

- the system treated this as a different worker even though product-wise it is
  still the same session-level `kst` fullagent flow
- cache continuity was broken again because worker identity is derived from the
  request text, not from the session-level singleton fullagent contract

## Root Cause

The current design still uses task identity to define worker identity.

### 1. Worker identity is dedupe-derived, not singleton-derived

`_build_child_state()` currently computes:

- `stable_dedupe_key = request.dedupe_key or normalize_dedupe_key(request.goal)`
- `runtime_session_id/runtime_session_key` from that dedupe key

This means wording changes such as:

- `执行 kst 指令，调用 pixiv-soft-r15 skill 找图并发送`
- `再次执行 kst 指令，调用 pixiv-soft-r15 skill 找图并发送`

produce different worker identities, even though the product wants one
fullagent for the whole session.

### 2. Reuse only checks active children

`GatewayOrchestratorSession.find_duplicate_child()` only scans `active_children`.

Once a child is archived into `recent_children`, the next fullagent turn is
free to create a new child with a new worker identity.

### 3. The system still thinks in terms of “many task workers”

`spawn_orchestrator_children()` still follows this logic:

- dedupe against active children
- otherwise create a new `GatewayChildState`
- if concurrency is available, start it as a fresh child

This is valid for a multi-worker orchestrator. It is the wrong model for “one
session, one fullagent”.

## Required Invariants

After the next implementation, all of the following must be true.

### Session-level singleton

- a NapCat session has exactly one fullagent worker identity
- all heavy/fullagent turns in the same session use that same
  `runtime_session_id`
- all heavy/fullagent turns in the same session use that same
  `runtime_session_key`

### Cache continuity

- the first fullagent turn in a session may be cold
- the next fullagent turn in the same session must reuse the same provider-side
  prompt-cache chain unless the cache TTL has genuinely expired or the prompt
  prefix changed for a legitimate reason
- wording differences in the user request must not create a new worker identity

### Lifecycle

- a finished fullagent worker remains reusable for later turns in the same
  session
- explicit reset/model-switch/clear-session must evict that worker cache
- no second fullagent worker may be created in the same session while the first
  one exists, regardless of dedupe wording

## Implementation Direction

### 1. Replace dedupe-based runtime identity with session-singleton identity

Do not derive fullagent runtime identity from `dedupe_key` or `goal`.

Use fixed helpers instead:

- `orchestrator_fullagent_session_id(parent_session_id)`
  - recommended shape:
    `"{sanitized_parent_session_id}__orchestrator_fullagent"`
- `orchestrator_fullagent_runtime_session_key(session_key)`
  - recommended shape:
    `"{session_key}::fullagent"`

These helpers should become the only runtime identity used for session-level
fullagent work.

### 2. Treat `GatewayChildState` as the latest task record, not as the worker identity

Keep `child_id` for:

- observability
- progress display
- transcript/result archival

But do not let `child_id`, `goal`, or `dedupe_key` define the runtime worker
identity anymore.

The runtime worker identity must belong to the session singleton.

### 3. Introduce explicit singleton worker ownership in session state

Add one session-level pointer for the singleton worker. One of these patterns is
acceptable; choose one and use it consistently:

- add `fullagent_runtime_session_id/fullagent_runtime_session_key` fields on
  `GatewayOrchestratorSession`
- or add a `singleton_fullagent` runtime record/object on
  `GatewayOrchestratorSession`

Required behavior:

- first fullagent turn initializes the singleton worker identity
- later fullagent turns always resolve to that same identity
- archived child records do not break worker reuse

### 4. Stop using `find_duplicate_child()` as the worker-reuse gate

For the singleton fullagent path:

- do not decide worker reuse via active-child dedupe
- always resolve to the session’s singleton fullagent runtime

`dedupe_key` can still be used for:

- deciding whether the user is continuing the current task
- status wording
- suppressing redundant queue entries

But it must not create a new runtime worker.

### 5. Route every heavy/fullagent request to the singleton worker

When the orchestrator decides a turn needs fullagent execution:

- if the singleton worker is idle, start a new task record on the existing
  singleton runtime
- if the singleton worker is active, either queue or interrupt according to the
  existing busy policy, but still keep the same runtime identity

Do not create a second fullagent child with a second runtime identity.

### 6. Preserve cache eviction boundaries

Keep the existing subtree cleanup logic, but extend/rename it to cover the new
singleton identity clearly.

The singleton fullagent cache must be evicted on:

- session reset
- `/model` switch
- branch/session replacement
- orchestrator `clear_session=True`
- compression-exhausted auto-reset

Do not evict it on normal child completion.

### 7. Keep observability explicit

Continue emitting per-task `child_id` events, but make the singleton runtime
identity visible in spawned/completed payloads so traces can prove reuse.

The expected pattern after the fix:

- different `child_id`
- same `runtime_session_id`
- same `runtime_session_key`

for every consecutive fullagent turn in the same session.

## Concrete Code Changes

### `gateway/napcat_orchestrator_support.py`

- add session-singleton fullagent runtime helpers
- stop using dedupe-derived helper paths for the singleton fullagent flow

### `gateway/napcat_orchestrator_runtime.py`

- `_build_child_state()`
  - bind fullagent tasks to the singleton runtime identity
  - stop deriving runtime identity from `stable_dedupe_key`
- `spawn_orchestrator_children()`
  - route fullagent requests to the singleton worker path
  - prevent creation of a second runtime identity in the same session

### `gateway/orchestrator.py`

- extend `GatewayOrchestratorSession` with explicit singleton worker ownership
- keep `active_children`/`recent_children` as task records, not worker records

### `gateway/run.py`

- keep cache retention for the singleton worker
- keep explicit eviction on reset/model-switch/session replacement
- rename helpers if needed so the code reflects “single fullagent worker”
  instead of “child subtree”

## Acceptance Tests

Add or update tests to prove the singleton behavior.

### Runtime identity tests

- two consecutive fullagent turns in the same session use the same
  `runtime_session_id`
- two consecutive fullagent turns in the same session use the same
  `runtime_session_key`
- goal wording changes such as `执行 kst` vs `再次执行 kst` do not change the
  runtime identity

### Cache lifecycle tests

- fullagent completion does not evict the singleton cached agent
- session reset/model switch/clear-session do evict the singleton cached agent

### Behavior tests

- only one fullagent worker may exist per session
- when a second heavy request arrives, it must reuse or queue behind the same
  singleton worker instead of creating a second runtime identity

### Trace-level validation

Reproduce a `kst` flow twice in the same session and verify:

1. first fullagent trace:
   - first child model call may have `cache_read_tokens = 0`
2. second fullagent trace:
   - first child model call must show `cache_read_tokens > 0`
3. both traces:
   - same `runtime_session_id`
   - same `runtime_session_key`

## Non-Goals

This handoff is intentionally not asking for the full blackboard/taxi rewrite in
the same change.

The minimum correct fix is:

- one session
- one fullagent runtime identity
- stable cache reuse across consecutive turns

If a future rewrite happens, it must preserve these invariants rather than
reintroducing task-derived worker identities.
