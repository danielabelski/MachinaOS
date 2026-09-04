# Memory Compaction, Session Token Tracking, and Cost Calculation

> **Related docs:** [memory_lifecycle.md](./memory_lifecycle.md) for the markdown / vector-store / state-clear surface. This doc is the SSOT for the **service** (`CompactionService`, thresholds, shared native summarization, and pricing). `memory_lifecycle.md` is the SSOT for the **flow** (how the markdown moves through an agent turn).

## Overview

`CompactionService` provides session token tracking, cost calculation, and
automatic memory compaction for agent executions that have a connected memory
session. Every provider normalizes reported usage into `services.llm.protocol.Usage`,
but only execution paths that call `CompactionService.track()` persist that
usage to the session metric tables. Standalone chat-model calls and arbitrary
third-party API requests are not part of this accounting surface.

All providers use the same client-side summarization path:
`CompactionService.compact_context()` calls
`run_native_llm_step(ChatUnifier, ...)`, then replaces the active memory
history with a structured five-section summary. The current agent runtime does
not enable Anthropic or OpenAI provider-managed compaction. Compaction reduces
active context pressure; it does not terminate an agent loop.

**Inspired by:** Claude Code's structured compaction pattern

**Default threshold:** per-user `UserSettings.compaction_ratio` > env
`Settings.compaction_ratio` (default **0.8** of the context window) > JSON
`llm_defaults.json:agent.compaction.ratio` fallback

**Cost calculation:** configured per-model pricing applied to usage the provider
reports; missing usage cannot be inferred

## Architecture

```
native agent loop
  └─ aggregates Usage across all LLM iterations
       ├─ in-process + connected memory
       │    └─ CompactionService.track()
       │         ├─ persist TokenUsageMetric + calculated cost
       │         ├─ update SessionTokenState
       │         └─ if threshold reached and memory/key available:
       │              compact_context()
       └─ Temporal AgentWorkflow
            ├─ returns aggregate usage in the workflow result
            └─ when its context counter reaches the prepared threshold:
                 agent.compact_context → compact_context()

compact_context()
  └─ run_native_llm_step(ChatUnifier, selected provider/model)
       └─ five-section summary → CompactionService.record()
            ├─ persist CompactionEvent
            ├─ reset the active-context counter
            └─ increment compaction_count
```

## Runtime Compaction Path

`compact_context()` uses the same native provider boundary as ordinary agent
turns. It sends one user message containing the current memory markdown through
`run_native_llm_step`, with SDK-internal retries disabled. The in-process path
allows the native step helper's bounded explicit retry policy; the Temporal
activity passes `explicit_max_retries=0` because Temporal owns activity
delivery and a repeated summarizer request can be billed twice.

`anthropic_config()`, `anthropic_api_config()`, and `openai_config()` remain
configuration helpers on `CompactionService`. The current chat and agent
execution paths do not pass those provider-managed compaction controls to the
SDK. `AgentWorkflow` currently calls `anthropic_config()` only as a
provider-agnostic way to calculate its numeric threshold.

## Database Schema

### TokenUsageMetric

Stores one metric whenever `CompactionService.track()` or the compaction
summarizer's internal metric writer is invoked. A memory-connected in-process
agent normally writes its loop aggregate as one row; the summarizer uses
`iteration=0`. This table is not populated by every LLM request in the system.

```python
class TokenUsageMetric(SQLModel, table=True):
    __tablename__ = "token_usage_metrics"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)        # Memory session ID
    node_id: str = Field(index=True)           # Agent node ID
    workflow_id: Optional[str] = None          # Workflow context
    provider: str                               # openai, anthropic, gemini, groq
    model: str                                  # Model identifier

    # Core token counts (native Usage contract)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    # Provider-specific token details
    cache_creation_tokens: int = 0   # Anthropic cache miss
    cache_read_tokens: int = 0       # Anthropic cache hit
    reasoning_tokens: int = 0        # OpenAI o-series reasoning

    iteration: int = 1               # Agent loop iteration number
    execution_id: Optional[str]      # Workflow execution ID
    created_at: Optional[datetime]

    # Cost columns (USD), computed by PricingService at track() time
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_cost: float = 0.0
    total_cost: float = 0.0
```

### SessionTokenState

Cumulative token state per memory session:

```python
class SessionTokenState(SQLModel, table=True):
    __tablename__ = "session_token_states"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(unique=True, index=True)

    # Cumulative counters (reset after compaction)
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_tokens: int = 0
    cumulative_reasoning_tokens: int = 0
    cumulative_total: int = 0

    # Compaction tracking
    last_compaction_at: Optional[datetime]
    compaction_count: int = 0

    # Stored per-session configuration (see the runtime caveat below)
    custom_threshold: Optional[int]
    compaction_enabled: bool = True

    updated_at: Optional[datetime]
```

### CompactionEvent

Historical record of compaction events:

```python
class CompactionEvent(SQLModel, table=True):
    __tablename__ = "compaction_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    node_id: str
    workflow_id: Optional[str]

    trigger_reason: str              # Always "native" today — record() hardcodes it
                                     # (compaction.py:233); no other value is written
    tokens_before: int
    tokens_after: int
    messages_before: int = 0
    messages_after: int = 0

    summary_model: str
    summary_provider: str
    summary_tokens_used: int = 0
    success: bool = True
    error_message: Optional[str]
    summary_content: Optional[str]   # The compacted summary

    created_at: Optional[datetime]
```

## Service API

### Initialization

The service is initialized via dependency injection in `container.py`:

```python
from services.compaction import get_compaction_service, init_compaction_service

# During app startup (handled by container)
compaction_service = init_compaction_service(database, settings)

# Get the singleton instance anywhere
svc = get_compaction_service()
```

### Track Token Usage

The in-process agent path calls this after a memory-connected native agent loop
returns aggregate usage:

```python
result = await svc.track(
    session_id="user-session-123",
    node_id="agent-node-1",
    provider="anthropic",
    model="claude-opus-4.6",
    usage={
        "input_tokens": 5000,
        "output_tokens": 1000,
        "total_tokens": 6000,
        "cache_creation_tokens": 2000,  # Optional
        "cache_read_tokens": 1500,      # Optional
        "reasoning_tokens": 0           # Optional
    }
)

# result:
# {
#     "total": 6000,           # New cumulative total
#     "threshold": 800000,     # Model-aware: 80% of 1M context window
#     "total_cost": 0.021,     # USD cost (cumulative)
#     "context_length": 1000000,  # Model context window the threshold was derived from
#     "needs_compaction": False
# }
#
# In-process threshold priority:
# custom_threshold > per-user UserSettings.compaction_ratio
# > env Settings.compaction_ratio (80%) > JSON fallback
```

### Record Compaction Event

`compact_context()` calls this after the shared client-side summarizer succeeds:

```python
await svc.record(
    session_id="user-session-123",
    node_id="agent-node-1",
    provider="anthropic",
    model="claude-3-5-sonnet-20241022",
    tokens_before=105000,
    tokens_after=15000,
    summary="## Summary\nConversation about project planning..."  # Optional
)
```

### Get Session Statistics

```python
# Model-aware threshold when model/provider given
stats = await svc.stats("user-session-123", model="claude-opus-4.6", provider="anthropic")
# {
#     "session_id": "user-session-123",
#     "total": 15000,
#     "threshold": 800000,  # 80% of 1M context window
#     "count": 1,  # Number of compactions
#     "context_length": 1000000  # 0 when no model/provider given
# }

# Without model/provider, falls back to global default
stats = await svc.stats("user-session-123")
# {"session_id": "user-session-123", "total": 15000, "threshold": 100000, "count": 1, "context_length": 0}
```

### Configure Per-Session Settings

```python
# Set custom threshold for a session
await svc.configure("user-session-123", threshold=50000)

# Disable compaction for a session
await svc.configure("user-session-123", enabled=False)

# Both
await svc.configure("user-session-123", threshold=75000, enabled=True)
```

These calls persist the fields, but their runtime effect is currently
path-dependent:

- `custom_threshold` is honored by `track()` and `stats()` on the in-process
  path.
- `AgentWorkflow` (F4.B) calculates a ratio-based threshold during
  `agent.prepare_payload`; it does not read `custom_threshold`.
- `compaction_enabled` is stored but is not consulted by `track()` or F4.B.
  Only the global `COMPACTION_ENABLED` value controls `track()` today, and F4.B
  does not currently honor that global enabled flag when it extracts the
  numeric threshold.

Treat the WebSocket settings as persistence/UI controls until those execution
paths are unified; do not promise that they disable or override every run.

## WebSocket Handlers

### get_compaction_stats

Get token usage statistics for a session:

```javascript
// Client request
ws.send(JSON.stringify({
    type: "get_compaction_stats",
    session_id: "user-session-123"
}));

// Server response
{
    "type": "get_compaction_stats",
    "success": true,
    "session_id": "user-session-123",
    "total": 45000,
    "threshold": 100000,
    "count": 0
}
```

### configure_compaction

Persist compaction settings for a session. See the path-dependent runtime
caveat under `Configure Per-Session Settings`; a successful response confirms
storage, not that every execution path consumed the setting.

```javascript
// Client request
ws.send(JSON.stringify({
    type: "configure_compaction",
    session_id: "user-session-123",
    threshold: 50000,
    enabled: true
}));

// Server response
{
    "type": "configure_compaction",
    "success": true
}
```

## Configuration

### Environment Variables

```bash
# In the repo-root .env (defaults live in the root .env.template:189-194)
COMPACTION_ENABLED=true       # Enable/disable compaction globally (default: true)
COMPACTION_RATIO=0.8          # Fraction of context window that triggers compaction
                              # (default: 0.8 — was 0.5 pre-2026.06). Range 0.05-0.99.
                              # Read by core.config.Settings.compaction_ratio.
```

**In-process `track()` threshold priority chain** (highest → lowest):

1. **Per-session** `SessionTokenState.custom_threshold` (set via `configure()` or WebSocket).
2. **Per-user** `UserSettings.compaction_ratio` × model's context_length — DB-backed override exposed in the Settings tab.
3. **Env** `Settings.compaction_ratio` × model's context_length — `COMPACTION_RATIO` × `model_registry.get_context_length(provider, model)`.
4. **JSON fallback** `llm_defaults.json:agent.compaction.ratio` × context_length — when Settings can't load (one-off CLI scripts).

The ratio-based threshold is computed from
`model_registry.get_agent_defaults()["compaction"]["ratio"]` and the model
context length — for example, a 1M-token model at 0.8 gets an 800K trigger.
F4.B uses this ratio-based calculation and does not apply the first
per-session step.

### Stored Per-Session Override

Sessions can store a custom threshold:

```python
# Via service API
await svc.configure("session-id", threshold=50000)

# Via WebSocket
ws.send(JSON.stringify({
    type: "configure_compaction",
    session_id: "session-id",
    threshold: 50000
}));
```

The custom threshold currently overrides the in-process `track()` and `stats()`
paths only; it is not read by F4.B `AgentWorkflow`.

## Integration with AI Service

### Token Extraction from Native Agent Responses

Every provider normalizes token accounting into
`services.llm.protocol.Usage`. The shared native agent loop adds usage from
every iteration, including cache and reasoning tokens, and returns the
aggregate in `final_state["usage"]`. Aggregation does not itself imply database
persistence:

```python
final_state = await run_native_agent_loop(...)
usage = final_state["usage"]

# services.llm.protocol.Usage(
#     input_tokens=8,
#     output_tokens=304,
#     total_tokens=312,
#     cache_creation_tokens=0,
#     cache_read_tokens=0,
#     reasoning_tokens=256,
# )
```

### In-Process Persistence Point

In `server/services/ai.py`, the in-process path calls the service only when a
legacy memory session id was resolved earlier in the run (`session_id` is set
from `memory_data["session_id"]` at `ai.py:1005`; it stays `None` once a
Context runtime resolves, because `execute_agent` nulls `memory_data` in that
case):

```python
# ai.py:1430-1446 — after native agent-loop execution, before memory save
if session_id and ai_response:
    compaction_result = await self._track_token_usage(
        session_id=session_id,
        node_id=node_id,
        provider=provider,
        model=model,
        ai_response=final_state.get("usage") or ai_response,
        all_messages=all_messages,
        broadcaster=broadcaster,
        workflow_id=workflow_id,
        memory_content=memory_data.get("memory_content", "") if memory_data else None,
        api_key=api_key,
        memory_node_id=memory_data.get("node_id") if memory_data else None,
    )
    _accumulate_compaction_usage(final_state, compaction_result)
```

## File Reference

| File | Description |
|------|-------------|
| `server/services/compaction.py` | `CompactionService` with session metrics, model-aware thresholds, and shared client-side summarization |
| `server/services/model_registry.py` | ModelRegistryService providing context_length for threshold computation |
| `server/models/database.py` | SQLModel tables for token tracking |
| `server/core/database.py` | CRUD methods for metrics and events |
| `server/core/config.py` | Environment variable configuration |
| `server/core/container.py` | Dependency injection setup |
| `server/services/settings/handlers.py` | WebSocket handlers `get_compaction_stats` (line 215) and `configure_compaction` (line 233); registered into `MESSAGE_HANDLERS` by `routers/websocket.py` |
| `server/main.py` | Service initialization on startup |

## Design Decisions

1. **One Native Summarization Path**: Every provider compacts through
   `run_native_llm_step(ChatUnifier, ...)`; the runtime does not enable
   provider-managed compaction.

2. **Pydantic BaseModel**: Use Pydantic for configuration validation to reduce boilerplate code.

3. **Per-Session State**: Each tracked memory session has independent
   counters. Stored per-session controls are not yet honored uniformly; see
   `Configure Per-Session Settings`.

4. **Model-Aware Threshold**: The ratio-based threshold is
   `compaction_ratio × context_window` (default **0.8**). The in-process
   `track()` path additionally lets `custom_threshold` override that value;
   F4.B currently does not.

5. **Singleton Pattern**: Service accessible via `get_compaction_service()` for easy integration anywhere in the codebase.

6. **Lazy Initialization**: Service is lazily initialized via container on first access, not blocking app startup.

## Shared Client-Side Compaction

For every provider, the service can perform client-side compaction when a
memory-connected execution reaches its applicable threshold. It uses
`ChatUnifier` through `run_native_llm_step` to generate a structured summary
following the five-section pattern. Summary `max_tokens` is capped at
`min(4096, model's max output tokens)`.

### compact_context() Method

```python
result = await svc.compact_context(
    session_id="user-session-123",
    node_id="agent-node-1",
    memory_content="# Conversation History\n...",  # Current memory markdown
    provider="anthropic",
    api_key="sk-...",
    model="claude-opus-4.6"
)

# result:
# {
#     "success": True,
#     "summary": "# Conversation Summary (Compacted)\n...",
#     "tokens_before": 105000,
#     "tokens_after": 0
# }
```

### Summary Structure

The compacted summary follows Claude Code's 5-section pattern:

```markdown
# Conversation Summary (Compacted)
*Generated: 2025-02-12T10:30:00Z*

## Task Overview
What the user is trying to accomplish.

## Current State
What's been completed and what's in progress.

## Important Discoveries
Key findings, decisions, or problems encountered.

## Next Steps
What needs to happen next.

## Context to Preserve
Details that must be retained for continuity.
```

### Automatic Triggering

On the in-process path, compaction is automatically triggered in
`_track_token_usage()` when:
1. `needs_compaction` returns true (cumulative tokens >= threshold)
2. Memory content is available (connected memory node)
3. API key is available for summarization

```python
# In server/services/ai.py _track_token_usage()
if tracking.get('needs_compaction') and memory_content and api_key:
    result = await svc.compact_context(
        session_id=session_id,
        node_id=node_id,
        memory_content=memory_content,
        provider=provider,
        api_key=api_key,
        model=model
    )

    if result.get("success"):
        # Update memory with compacted summary
        memory_data['memory_content'] = result['summary']
```

F4.B performs a parallel check inside `AgentWorkflow` using the workflow's
active-context usage counter and the ratio-based threshold recorded by
`agent.prepare_payload`. It invokes the same `compact_context()` method
through the `agent.compact_context` activity
(`services/temporal/agent_activities.py:1944`).

### AI Service Wiring

The compaction service requires the AI service to generate summaries. This is wired during app startup:

```python
# In server/main.py
from services.compaction import get_compaction_service

compaction_svc = container.compaction_service()
compaction_svc.set_ai_service(container.ai_service())
```

## WebSocket Broadcasts

The in-process integration can broadcast real-time updates to the frontend:

### token_usage_update

Broadcast after a memory-connected in-process execution successfully tracks
usage:

```json
{
    "type": "token_usage_update",
    "session_id": "user-session-123",
    "data": {
        "total": 45000,
        "threshold": 100000,
        "needs_compaction": false
    }
}
```

### compaction_starting

Broadcast when compaction is about to begin:

```json
{
    "type": "compaction_starting",
    "session_id": "user-session-123",
    "node_id": "agent-node-1"
}
```

### compaction_completed

Broadcast when compaction finishes:

```json
{
    "type": "compaction_completed",
    "session_id": "user-session-123",
    "success": true,
    "tokens_before": 105000,
    "tokens_after": 0,
    "error": null
}
```

## Frontend UI

### Token Usage Panel

The Token Usage panel is displayed in the MiddleSection of the parameter panel
for **agent nodes that have a memory session connected** — the gate is
`isAgentWithSkills && connectedMemorySessionId`
(`client/src/components/parameterPanel/MiddleSection.tsx:767-768`), not the
memory node itself. It shows:

- **Progress bar**: tokens used vs the model's context length when known (`compactionStats.context_length`), else vs the threshold; turns destructive at 80%
- **Statistics**: `Total`, editable `Threshold`, `Compactions`, and `Context` (when a context length is known), plus the session id
- **Editable threshold**: click the `Threshold` stat to reveal a number input + save button; saving fires `configure_compaction` for the connected session

Built from shadcn primitives (no antd):

```typescript
// client/src/components/parameterPanel/MiddleSection.tsx:768-870 (abridged)
{isAgentWithSkills && connectedMemorySessionId && (
  <Accordion type="single" collapsible defaultValue="tokens">
    <AccordionItem value="tokens">
      <AccordionTrigger>Token Usage {total}K / {displayMax}K</AccordionTrigger>
      <AccordionContent>
        <Progress value={Math.min(percent, 100)} className={cn(isWarning && '[&>div]:bg-destructive')} />
        <Stat title="Total" value={compactionStats.total} />
        {isEditingThreshold ? (
          <>
            <Input type="number" value={editThresholdValue} min={10000} max={2000000} step={10000} />
            <ActionButton intent="save" onClick={() => configureCompactionMutation.mutate({ sessionId, threshold })} />
          </>
        ) : (
          <Stat title="Threshold" value={compactionStats.threshold} />   {/* click to edit */}
        )}
        <Stat title="Compactions" value={compactionStats.count} />
        {hasContext && <Stat title="Context" value={`${Math.round(ctxLen / 1000)}K`} />}
      </AccordionContent>
    </AccordionItem>
  </Accordion>
)}
```

## Future Enhancements

1. **Compaction History UI**: View past compaction events and summaries in the frontend
2. **Multiple Summary Strategies**: Allow users to choose different summarization approaches
