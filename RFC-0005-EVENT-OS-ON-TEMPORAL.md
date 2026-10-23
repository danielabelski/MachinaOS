# RFC-0005 — Event OS: Event-Driven AI Employees on Temporal

- **Status:** Accepted, 2026-10-23 (owner decisions in §3). Implementation is phased (§24). Phase 0 landed on 2026-10-23 (§24.4); Phase 1 is next.
- **Created:** 2026-10-23
- **Scope:** the path from every event source to every effect:
  - `server/services/events/` and `server/routers/webhook.py`;
  - the receive and send paths of the channel plugins (`server/nodes/{telegram,whatsapp,whatsapp_business,discord,twitter,email,google,stripe,dodo_payments,...}`);
  - `server/services/chat/` and `server/services/deployment/`;
  - `server/services/temporal/`: new workflow types, and changes to MachinaWorkflow and AgentWorkflow behind input flags;
  - `server/core/database.py` and `server/services/status_broadcaster.py`;
  - the deploy logic (`cli/terraform/`, `cli/commands/deploy/`, `install.sh`, `docker/`).
- **Relationship:**
  - Implements, for the event path, RFC-0004 decisions D7 (bounded events), D8 (routing by known ids), D9 (cancellation), D10 (upgrade contract), D13 (human waits) and D18 (security fixes).
  - Keeps RFC-0002's conversation store and adds a thread to its key.
  - Until a phase lands, [event_framework.md](./docs-internal/event_framework.md), [chat_protocol.md](./docs-internal/chat_protocol.md), [temporal-workflow-control.md](./docs-internal/temporal-workflow-control.md) and [TEMPORAL_ARCHITECTURE.md](./docs-internal/TEMPORAL_ARCHITECTURE.md) stay normative for what it covers.
- **Baseline:** HEAD a8e390ca (2026-10-23), temporalio 1.30.0, Temporal CLI unpinned. Line numbers are as of that date and drift. Figures marked *est.* are unmeasured and are confirmed by the gates in §26.

## How to read this

- **Owner:** §1, §3 (decisions), §25 (defaults chosen and what is still open).
- **Reviewer:**
  - §4: what is broken today and where;
  - §6: the architecture and its invariants;
  - §9.5: the delivery protocol;
  - §23: failure modes.
- **Implementer:** §7 to §21 by layer, §22 (scenarios), §24 (phases), §26 (acceptance gates).

## 1. Summary

OpenCompany's AI employees should run like the processes of an operating system that sleeps until something happens. A message, a webhook, a timer or another employee wakes one. It reacts, schedules its own follow-ups, shares the machine fairly with every other employee, and delivers what it produces through queues that apply backpressure instead of dropping. This RFC designs that system on Temporal, borrowing the Linux kernel's answers to the same problems:
- interrupt top and bottom halves;
- NAPI's switch from push to pull under load;
- scheduling classes with reserved shares;
- epoll-style match rules;
- timer wheels;
- qdisc and BQL on the way out;
- supervisors that cap restart intensity;
- cgroup memory limits applied to model spend.

**Today's event path is at-most-once.** Every event is routed by a Visibility query that is eventually consistent by design. When Temporal is down, a query lags or a Signal fails, the event is dropped with a log line while the producer is told it succeeded. Telegram discards every message sent while the backend was down. One large webhook can wedge a deployment. Start nodes fire again on every restart. Inbound message bodies are broadcast to every connected socket. (§4)

**The design** (§6):
1. **Ingress commits before acknowledging.** Every event is appended to an inbox in the instance's own database (SQLite on the desktop, PostgreSQL on hosted instances) before anything acknowledges it. Nothing else happens in the request path.
2. **A router (DB only) assigns streams.** Match rules compiled from each generation's graph give every input a place in one conversation stream, under per-stream sequence numbers.
3. **A relay delivers by known id.** It sends each stream's pending inputs in bounded batches to a deterministic Temporal entity, one Signal-With-Start in flight per stream, and records progress only after Temporal accepts. Duplicates are dropped by sequence number. An input counts as consumed in the same transaction that saves it into the agent's history.
4. **Each conversation is one entity.** An entity workflow (`ConversationWorkflow`) debounces bursts, starts one turn at a time, and offers new messages to the running turn at its next step.
5. **An employee supervisor workflow holds control and time.** It owns control state, alarms and the daily budget.
6. **Classes and budgets decide who runs.** Four scheduling classes with reserved shares (owner, customer, internal, background) and a daily budget per employee decide who runs when capacity or money is short.
7. **Timers live in Temporal.** Cron jobs and polls are Temporal Schedules addressed by immutable ids. Reminders are timers in the supervisor.
8. **Employees talk through the inbox.** Messages between employees are inbox rows carrying causal ids, a hop limit and a breaker.
9. **Sends go through an outbox.** Each send has an idempotency key, and its token buckets slow the producing turn instead of dropping. A send whose outcome is uncertain is shown as `unknown`, never resent on its own.
10. **Temporal is rebuildable.** Everything needed to resume lives in the app database, so Temporal can be rebuilt from it (I10). An instance can therefore move from the free dev server to the free open-source server, or recover a lost Temporal database, without losing an acknowledged event.

**Deployment model (owner decisions, §3).**
- Every owner runs their own instance, each with free Temporal: a desktop app, a container or a VM. There is no shared cluster and no Temporal Cloud.
- Instances with up to about 100 employees use the CLI dev server. Larger hosted instances, up to about 1,000 employees, use the open-source server on the instance's PostgreSQL.
- Releases reach the hosted fleet together in a weekly window.
- Instances report metrics and errors, never content.

**Rollout.** Phase 0 ships nine fixes for verified defects without changing any workflow command. Phase 1 builds the replay safety net. Phases 2 to 8 introduce the inbox, drivers, entities, scheduler, timers, messaging, egress and supervision, each versioned per generation. Phase 9 covers instance tiers and fleet operations. Phase 10 retires the old path. (§24)

## 2. Method and sources

- **Research.** Fifteen parallel research passes (RFC-0004's Temporal mirror and audits) and four new parallel design passes (current-state audit, target architecture, bus and scale, migration).
- **Verification.** Claims that changed the design were checked against the code at HEAD; the citations in §4 were re-read.
- **Temporal sources:**
  - a local mirror of 282 docs.temporal.io pages (2026-10-16), especially message passing, entity workflows, the event-accumulator pattern, Schedules, task-queue priority and fairness, multi-tenant patterns, the CLI server reference, the upgrade guide and the production checklist;
  - the temporalio 1.30.0 source (`temporalio/worker/_tuning.py`, `temporalio/common.py`);
  - forum threads on signal throughput and Kafka-to-Temporal consumers.
- **Linux sources:**
  - the kernel documentation for NAPI, generic IRQ handling, EEVDF and CFS scheduling, cgroup v2 (`cpu.max`, `memory.high`, `memory.max`), PSI, timers, qdiscs and BQL, RCU and the soft-lockup watchdog;
  - the `epoll`, `inotify` and `netlink` man pages;
  - systemd's `Restart=`, `StartLimit*=` and `Persistent=`.
- **Agent-OS prior art:**
  - AIOS (arXiv 2403.16971), Letta, Erlang/OTP supervisors, Orleans grains and reminders, Akka mailboxes;
  - peer platforms: Inngest (debounce, batch, throttle), Restate (virtual objects), Hatchet (concurrency strategies), LangGraph (double-texting), DBOS (partitioned queues).
- **Reference systems built on Temporal:** Temporal's chatbot sample (one workflow per conversation), Cursor's agent runs (Signal-With-Start follow-ups, replayed histories in CI), Quo's voice agent (interruptions as Signals).
- **Not done:** no load test and no run against a live server. Throughput figures are estimates.

## 3. Decisions

### 3.1 Owner decisions (2026-10-23)

| # | Decision |
|---|---|
| E1 | **One instance per owner.** Every owner's desktop app, container or VM runs OpenCompany, its database and its own free Temporal. No shared hosted backend, no Temporal Cloud. Target: 100,000+ employees across about 10,000 instances. |
| E2 | **Instance tiers.** The desktop and hosted instances up to about 100 employees run the Temporal CLI dev server. A hosted instance past about 100 employees (up to about 1,000) moves to the free open-source Temporal server on its own PostgreSQL. (§20) |
| E3 | **Databases.** Hosted instances run PostgreSQL for the app database and the inbox. The desktop app stays on SQLite. |
| E4 | **Bus.** The inbox in the instance's database is the only durable store for events. gRPC carries events where a transport is needed: the protocol for out-of-process drivers (the WhatsApp bridge first), with acks and resume, and the relay's calls into Temporal. No message broker. (§7.3, §9) |
| E5 | **Timers.** Cron jobs, polling triggers and reminders run on Temporal Schedules and timers. (§15) |
| E6 | **Merging.** A new message for an employee that is still working is merged into the running work at its next step. Quick bursts are batched into one input. (§11) |
| E7 | **Internal wake sources:** messages from other employees, self-scheduled wake-ups, task results and delegations, and changes to watched shared data. (§16) |
| E8 | **Employee messaging.** Any employee may message, delegate to and wake any other employee of the same owner without linking. Loop limits apply. Owners live in separate instances, so traffic between owners cannot happen. |
| E9 | **Customer histories.** Each customer conversation keeps its own history. Owner chat stays one conversation. Shared facts go through the Memory tool. (§12) |
| E10 | **Priority.** When capacity is short the order is: owner chat and approvals, then customer conversations, then internal events, then background work. Every class keeps a reserved share. (§13) |
| E11 | **Daily budget per employee.** Past 80%, only owner and customer conversations run. At 100% the employee pauses and the owner gets a notice with Resume. It resumes on its own at midnight in the owner's time zone. (§14) |
| E12 | **Uncertain sends.** A send whose outcome is unknown, to a provider without idempotency keys (WhatsApp bridge, Telegram), is never resent automatically. It shows `unknown` with a Retry button. (§17) |
| E13 | **Fleet upgrades.** Releases reach all hosted instances together in a fixed weekly maintenance window. (§21) |
| E14 | **Telemetry.** Hosted instances report metrics and errors centrally, never content. (§18.5) |
| E15 | **The Linux model** draws on all three diagrams: the kernel map, the network stack and the storage stack. |

### 3.2 RFC-0004 decisions this RFC implements

| RFC-0004 | Decision | Here |
|---|---|---|
| Q7 / D8 | Guaranteed delivery: route by the app database to known ids, retry, answer 5xx when not stored | §8, §9 |
| Q1 / D10 | Runs open on release N-1 survive an upgrade | §24.1, §24.3 |
| Q4 / D12 | Failed runs end as Failed workflows | §10 |
| Q10 / D13 | Human waits become an Update plus a durable timer | §17.5 |
| Q12 | Preview features only behind a flag, off by default | §10.4 |
| Q16 | The Start node runs once per generation | Phase 0.4 |
| Q8 | Sends: at-least-once with idempotency keys where the provider supports them, otherwise at-most-once (E12) | §17 |

RFC-0004 Q2 (which server durable installs run on) is answered by E1 to E3.

## 4. Current state (audit at HEAD, 2026-10-23)

### 4.1 How an event flows today

- **Entry point.** Every source calls `emit` in `server/services/events/dispatch.py:63-106`.
- **Routing.** `emit` runs one Visibility List query per event to find running consumers (`:132-164`), then sends a plain Signal `on_event` to each one (`:250-257`).
- **The consumer.** The consumer is the deployment's `WorkflowControlWorkflow`. It:
  - dedupes the last 4,096 keys;
  - queues FIFO per generation;
  - spawns one `MachinaWorkflow` per event, one at a time, with no cap on concurrent runs (`server/services/temporal/workflow_control_workflow.py:336-484`; `trigger_listener_workflow.py:305-512`).
- **Outside the controller:**
  - Cron uses Schedules that start runs directly.
  - Polling runs inside the controller.
  - Owner chat admits a run in the chat ledger, then emits.

### 4.2 Guarantees today

- **At-most-once ingress.** An event is dropped with a log line when Temporal is down, the Visibility query fails or lags, no consumer is found, or a Signal fails (`dispatch.py:125-206`). Producers always see success: webhooks answer 200 regardless (`server/routers/webhook.py:69-106`).
- **Retries are never deduped for generic webhooks.** They get a random event id. Signed providers (Stripe `evt_`, Dodo `webhook-id`, WhatsApp Business `wamid`) have stable ids.
- **One run per chat session.** Owner chat allows one live run per session, and a second message is refused with `run_in_progress` (`server/services/chat/ledger.py:286-293`). WhatsApp and Telegram messages each start their own run, so replies in one conversation can race.
- **No priority and no per-employee cap.**
  - LLM steps run on `machina-tasks`, not the rate-limited `ai-heavy` queue (RFC-0004 M9).
  - Provider rate limits are per worker (`server/services/temporal/worker.py:618`).
  - One deterministic spawn failure retries the head of a generation's queue every 5 s and blocks everything behind it (`workflow_control_workflow.py:477-480`).
- **Internal events.** Only `agent.task.completed` can wake work, and it is unscoped (H11). An employee cannot schedule its own wake-up or message another employee.

### 4.3 Defects this RFC fixes

| Id | Defect | Evidence | Fixed in |
|---|---|---|---|
| C1 (RFC-0004) | One large webhook wedges a deployment: the body is copied about four times into a child's input, past the 2 MB payload limit | `trigger_listener_workflow.py:394-397`; `workflow_control_workflow.py:705-711` | 0.3, controller lane, P4 |
| C3 (RFC-0004) | Webhook header authentication is never enforced, and an empty path matches every webhook | `server/routers/webhook.py`; `nodes/trigger/webhook_trigger` filter | 0.1 |
| C4 (RFC-0004) | A deployment's Start node fires again on every backend restart | `server/services/deployment/handlers.py:1612-1617`; `manager.py:282` | 0.4 |
| H1 (RFC-0004) | Routing through Visibility drops events silently | `dispatch.py:109-206` | P2 |
| H6 (RFC-0004) | Cron overlap is not prevented: the Schedule's policy covers only the short workflow it starts | `server/services/temporal/schedules.py` | P6 |
| H8 (RFC-0004) | Workflow loops swallow cancellation | RFC-0004 §5 | controller lane |
| H11 (RFC-0004) | Task events reach every workflow and every socket | `server/nodes/agent/_events.py:109-134` | 0.2, P6 |
| M3 (RFC-0004) | Polling loses events: a failed detail fetch is marked seen, and mark-as-read runs before the event is recorded | `server/services/plugin/polling.py:233-259` | P3 |
| N1 | Telegram discards every message sent while the backend was down (`drop_pending_updates=True`) | `server/nodes/telegram/_service.py:663` | 0.5, P3 |
| N2 | The WhatsApp bridge loop awaits `emit` inline and acknowledges nothing | `server/nodes/whatsapp/_service.py:111-127` | P3 |
| N3 | Discord interactions are acknowledged, then dispatched fire-and-forget | `server/nodes/discord/_router.py:110-117` | P2 |
| N4 | With login on, Discord's interaction POSTs get 401 (not a public path) | `server/middleware/auth.py:14-34` | 0.7 |
| N5 | A deployed `twitterReceive` never fires: its dispatcher is never called | `server/nodes/twitter/_events.py:68` | 0.8 |
| N6 | On PostgreSQL the chat lane index is a full `UNIQUE(session_id)`, so a conversation's second run fails | `server/models/chat.py:69` (only `sqlite_where`) | 0.6 |
| N7 | Inbound bodies are broadcast to every socket, twice for WhatsApp (raw frame plus envelope) | `server/services/status_broadcaster.py:228-281`; `server/nodes/whatsapp/_events.py:214-223` | 0.9 |
| N8 | Every reserved transaction on PostgreSQL takes one global advisory lock: about 60 call sites and 8-12 per turn, so an instance tops out at est. 20-50 turns/s | `server/core/database.py:530`, `:763` | P9b |
| N9 | `workflow.db` has no WAL and no `busy_timeout`; a long reader blocks every commit | only `server/services/temporal/_runtime.py:153-158` sets WAL, and only for Temporal | P2 |
| N10 | PostgreSQL has no schema migrations: a missing column fails startup | `server/core/database.py:193` | P9b |
| N11 | A webhook source puts every event on a queue nothing drains | `server/services/events/webhook.py:121` | 0.3 |

## 5. Principles from the Linux stack

### 5.1 Eight principles

1. **Sleep by default; wake on an edge, reconcile the level.** An event is a hint and state is the truth (NO_HZ idle, Orleans virtual actors). A waiting Temporal workflow holds no worker.
2. **Split every event into a tiny top half and a deferred bottom half.**
   - The top half authenticates, dedupes, enqueues and acks.
   - The bottom half is schedulable and may sleep (hard IRQ, then softirq, workqueue or threaded IRQ).
   - Never do slow work at the edge.
3. **Bound every queue and name its overflow policy:** block, coalesce, drop-oldest, or reject plus resync (inotify `IN_Q_OVERFLOW`, netlink `ENOBUFS`). Queue wait time is the primary health signal (PSI, the USE method).
4. **Under load, switch from push to pull and drain a batch against a budget.** NAPI: the first interrupt arms a poll that drains up to a budget before interrupts are re-enabled.
5. **Schedule with strict classes, weighted shares, quotas and reserved floors** (stop, deadline, rt, fair and idle classes; EEVDF weights; `cpu.max`; RT throttling keeps 5% for others). Running work is not preempted, so isolate capacity per class. Bound the depth of downstream queues to protect latency (Kyber, BQL).
6. **Failure is normal.** Supervise hierarchically, back off, cap restart intensity and escalate (systemd `Restart=` and `StartLimitBurst=`, OTP supervisors' MaxR/MaxT). Enforce budgets before thrash: `memory.high` throttles, `memory.max` stops.
7. **Storms need circuit breakers that degrade gracefully** (spurious-IRQ disable, journald rate limits). Agent form: hop and TTL limits, causal ids, a token bucket per edge, and a breaker that turns a hot edge into a periodic digest.
8. **Instrument every edge and measure waiting, not just utilisation** (tracepoints, PSI).

### 5.2 Mapping

| Linux | Agent OS | Here |
|---|---|---|
| process, `task_struct` | an employee and each of its conversations | supervisor and conversation entity workflows (§10) |
| device model: bus, driver, device, probe, hotplug | channel adapters bound to employees | `ChannelDriver` registry (§7) |
| hard IRQ / bottom half | ingress / conversation turn | durable append and ack (§8), then router and relay (§9) |
| NAPI | doorbell and batch drain | at most one delivery in flight per stream, a bounded window, backlog left in the database (§9.4) |
| signal delivery on return to user space | merging new messages | the agent loop's step boundary (§11) |
| scheduling classes, cgroups, HTB | owner, customer, internal, background | Priority keys, class queues, a slot pool with floors, admission (§13) |
| `memory.high` / `memory.max` | spend throttle and pause | daily budget ledger (§14) |
| epoll interest lists, D-Bus match rules, netlink groups | subscriptions | `bus_subscriptions` compiled at Start (§9.3) |
| timer wheel, hrtimers, systemd timers | cron, polls, reminders | Schedules and durable timers (§15) |
| pipes, signals, D-Bus | employees messaging each other | inbox rows with causal ids (§16) |
| qdisc (TBF, HTB, fq_codel), BQL, writeback throttling | the outbound path | outbox, token buckets, the producer slows down (§17) |
| RCU | a running generation keeps its version | `resource_manifest` flags frozen per generation (§10.4) |
| watchdogs, OOM killer, oomd | stuck and runaway employees | deadlines, restart intensity, quarantine, budgets (§18) |
| journaling (jbd2) | write-ahead before acknowledging | inbox append before any ack (I2) |

### 5.3 Prior art adopted

- **AIOS:** per-resource queues and syscalls with priority and time limits; confirmation before irreversible actions (approvals).
- **Letta:** loop control belongs to the runtime, not the model.
- **Erlang/OTP:** asynchronous messages, let it crash, supervision with restart intensity.
- **Orleans:** virtual actors that activate on demand, durable reminders, turn-based single-threaded execution.
- **Akka:** bounded mailboxes that overflow to dead letters, control-aware priority mailboxes, passivation of idle entities.
- **Peer platforms:**
  - Inngest debounce, batch and throttle are the entity's debounce and the class buckets.
  - LangGraph's double-texting strategies (reject, enqueue, interrupt, rollback) and Hatchet's `CANCEL_IN_PROGRESS` are the entity's merge, next-turn and Interrupt.
  - Restate virtual objects are entity workflows.
  - DBOS partitioned queues are class queues plus fairness keys.

## 6. Architecture

### 6.1 Shape

```
DRIVERS (RX)          TOP HALF (request path)     BOTTOM HALF (database)          RELAY                           PROCESSES (Temporal)                         EGRESS (TX)
WhatsApp bridge  ->   verify, cap, normalize, ->  router: subscriptions,     ->  per conversation stream:   ->   ConversationWorkflow per conversation: ->    outbox row ->
(gRPC), Telegram,     dedupe, INSERT              storm guards, stream seq,       gates (generation, budget),     debounce, admit, turn child,                 send activity
Discord, webhooks,    bus_ingress, commit, ack    bus_deliveries                  window, Signal-With-Start       merge at step boundaries                     (token bucket,
polls, schedules,     (no Temporal, no LLM)                                       "deliver" with the batch        EmployeeSupervisorWorkflow per employee:     priority,
IPC, owner chat                                                                                                   control, alarms, budget, upkeep              idempotency key)
```

### 6.2 Invariants

| # | Invariant | Principle |
|---|---|---|
| I1 | A Signal is a hint. The inbox and its cursors are the truth; every consumer reconciles from them. | 1 |
| I2 | No request path calls Temporal or a model. Every request path ends at a durable commit. | 2 |
| I3 | Every queue is bounded and names its overflow policy. Every drop is a row with a reason. | 3 |
| I4 | One relay per stream at a time and one consumer per stream. An entity runs at most one turn, and an input belongs to exactly one turn. | — |
| I5 | Serialize only where state is shared (an agent's conversation key). Everything else runs in parallel under class floors. | 5 |
| I6 | Routing goes from the app database to known workflow ids. Visibility is for operations only. | — |
| I7 | Nothing over 64 KB enters Temporal; larger bodies travel by reference. An entity holds at most 32 inputs or 512 KB, and any backlog stays in the database. | 4 |
| I8 | Every event carries `chainid`, `hop` and `causationid`. Internal wakes are checked for hops, cycles and rate. | 7 |
| I9 | Every effect is idempotent, or at-most-once with an explicit `unknown`. Never a silent duplicate. | — |
| I10 | Temporal is rebuildable from the app database: generation snapshots, conversation histories, inbox cursors, alarms, budgets and Schedule definitions all live there. | 6 |

### 6.3 Layers

| Layer | Linux analog | Responsibility | Runs in | Section |
|---|---|---|---|---|
| Drivers | device model, NIC driver | probe, start/stop, normalize, ack after commit, send | app process; edgymeow (Go) | §7 |
| Ingress | hard IRQ | verify, cap, dedupe, append, ack | request path | §8 |
| Router | softirq, match rules | subscriptions, storm guards, stream sequence numbers | in-process task | §9.3 |
| Relay | receive queue, TCP window | gates, window, Signal-With-Start, `delivered_seq` | in-process task | §9.4 |
| Processes | task_struct, run queue | supervisor, conversation entity, turn, agent | Temporal | §10 |
| Scheduler | sched classes, cgroups, HTB | classes, floors, admission, Priority, fairness | DB + Temporal | §13 |
| Budgets | `memory.high` / `memory.max` | daily ledger, holds, pause, auto-resume | DB + supervisor | §14 |
| Timers | timer wheel, systemd timers | Schedules, alarms, durable timers | Temporal | §15 |
| IPC | pipes, D-Bus | employee messages, delegations, watched data | inbox | §16 |
| Egress | qdisc, BQL | outbox, buckets, idempotency, approvals | Temporal activities + DB | §17 |
| Supervision | systemd, OTP, watchdogs, PSI | deadlines, restart intensity, attention, metrics | supervisor + DB | §18 |

## 7. Drivers (the device model)

### 7.1 Contract

A `ChannelDriver` implements:
- `probe(account)` and `start(account)` / `stop(account)`;
- `normalize(raw) -> IngressRecord` (envelope, dedupe key, conversation id, counterpart, size);
- `ack(raw)`, called only after the inbox append has committed;
- `send(outbound) -> SendResult`;
- a `capabilities` declaration: who holds unacknowledged events, ordering, send idempotency, reply window.

The `DriverRegistry` runs a connection driver only while a running generation subscribes to its account (principle 1). It re-probes on credential events (`com.opencompany.credential.*`) and on generation Start or Reset. A lease in `bus_leases` guarantees one reader per account; Telegram, for example, allows only one `getUpdates` consumer per token. An account that stays down for more than 5 minutes becomes an attention item.

Plugins keep their own drivers in `server/nodes/<plugin>/_driver.py` and register them from their package `__init__.py`, like the other plugin registries. The old `register_webhook_source`, `register_filter_builder` and `register_trigger_precheck` remain as shims for one release.

### 7.2 Per-driver behaviour

| Driver | Who holds unacked events | Dedupe key | Ack after commit | Send idempotency |
|---|---|---|---|---|
| WhatsApp (edgymeow bridge) | WhatsApp's server, through whatsmeow `SynchronousAck` plus the bridge's outbox | `whatsapp:<acct>:<chat>:<msgid>` | gRPC `Ack(seq)`; the bridge then acks WhatsApp | none: at-most-once (E12) |
| WhatsApp Business | Meta, retried up to 7 days | `wamid` | 200 | none; 24-hour reply window |
| Telegram | Telegram, 24 hours | `update_id` | the `offset` advances only after commit | none |
| Discord gateway | gateway resume by sequence; REST catch-up after a full reconnect | message id | n/a | `nonce` with `enforce_nonce` |
| Discord interactions | not redelivered; 3-second window | interaction id | deferred reply (type 5) after commit, else 500 | n/a |
| Owner chat | the client re-sends with `client_message_id` | message uid | WebSocket answer after commit | database |
| Signed webhooks (Stripe, Dodo, GitHub, Standard Webhooks, WhatsApp Business) | the provider (Stripe retries for 3 days) | `evt_…`, `webhook-id`, `X-GitHub-Delivery` | 2xx | the provider's `Idempotency-Key` |
| Generic webhook | the caller | a configured header, else a body hash within 10 minutes | 2xx | caller-defined |
| Pollers (Gmail, IMAP, Outlook, Twitter) | provider state | message id, UID, delta token, tweet id | cursor saved in the append transaction; `post_emit` (mark-as-read) after commit | email `Message-ID` check |
| Cron, Start, alarms | Temporal | `<schedule>:<scheduled_time>`, `start:<gen>`, `alarm:<id>` | n/a | n/a |
| Internal (messages, tasks, data changes) | the database | `<producer workflow id>:<activity id or tool_call_id>` (never the run id) | n/a | n/a |

### 7.3 gRPC driver protocol (E4)

Out-of-process drivers stream events into the bus over gRPC: on loopback or a named pipe on the desktop, and on localhost with mTLS where the driver runs in another container.

```proto
service EventIngress {
  // The driver resends every unacknowledged event after a reconnect, in order.
  rpc Publish(stream DriverEvent) returns (stream Ack);
}
message DriverEvent {
  string driver_id = 1;    // "whatsapp:<account>"
  uint64 seq = 2;          // per-driver monotonic sequence from the driver's outbox
  string source_key = 3;   // account scope
  string dedupe_key = 4;   // e.g. whatsapp:<acct>:<chat>:<msgid>
  string type = 5;         // CloudEvents type
  bytes envelope = 6;      // CloudEvents JSON, at most 64 KB
  string body_ref = 7;     // for media and large bodies
}
message Ack { uint64 seq = 1; enum Status { ACCEPTED = 0; DUPLICATE = 1; REJECTED = 2; } Status status = 2; string reason = 3; }
```

- The bus sends `Ack` only after the append commits.
- At most 1,000 events are in flight per stream, with deadlines on every call.
- The driver advances its upstream cursor only after the `Ack`: the WhatsApp acknowledgement, the Telegram offset, the Discord sequence, the IMAP UID.

**edgymeow changes.** edgymeow is its own Go project, published to npm and pinned at `edgymeow@0.0.20` in `server/nodes/whatsapp/_install.py:39`. It changes in its own repository:
- Turn on whatsmeow `SynchronousAck`.
- Write each decrypted event to a local SQLite outbox before acknowledging WhatsApp.
- Stream the outbox over `Publish`, and delete each row on `Ack`.
- Resend from the last acknowledged sequence after a reconnect.

Today the bridge speaks JSON-RPC over WebSocket without acks (`server/nodes/whatsapp/_service.py:111-127`).

gRPC is not used as the bus itself, and no router-side log is built (§9.7).

### 7.4 Driver-level fixes

- **Telegram:** a manual `get_updates(offset)` loop replaces `start_polling(drop_pending_updates=True)`.
- **WhatsApp:** the `_recv` loop only appends (the bridge protocol above).
- **Discord interactions:** append before the deferred reply.
- **Pollers:** a failed detail fetch is not marked seen, and becomes a dead row after 5 failures.
- **Twitter receive** joins the polling path.

## 8. Ingress (the top half)

### 8.1 Pipeline

1. **Resolve the endpoint** from the active generations' subscriptions.
2. **Check size.** Check `Content-Length`, then stream the body with a cap: 256 KiB by default, raisable per endpoint up to 10 MiB.
3. **Verify** with a constant-time comparison of the configured header, or an HMAC over the raw bytes for signed providers.
4. **Normalize** to a CloudEvents envelope. Keep allowlisted headers only; never `authorization`, `cookie`, `proxy-authorization` or the configured secret header.
5. **Store large bodies by reference.** A body over 64 KB is streamed to `event_blobs` (a file plus its sha256). The row keeps `body_ref` and a 4 KB preview.
6. **Append.** `INSERT ... ON CONFLICT (source_key, dedupe_key) DO NOTHING`, commit, and answer.

**Never `reserved_session`.** Ingress uses its own short transaction. On PostgreSQL, `reserved_session` takes the global advisory lock (N8).

**Owner chat.** `admit_message` (`server/services/chat/ledger.py:252`) writes the chat message and the ingress row in one transaction, so it becomes the transactional outbox.

**Internal producers** (the tools in §16, `alarms.fire`, task completion) append with deterministic keys from inside their activity. A failed append raises, so Temporal retries the activity.

`dispatch.emit()` remains as a facade that appends an internal ingress row, so existing producers keep working.

### 8.2 Answers

| Condition | Answer |
|---|---|
| Appended, or a duplicate | 200 (Discord: deferred reply, type 5) |
| Body over the endpoint cap | 413, before reading when `Content-Length` is known |
| Bad signature or header secret; an empty generic path | 401 |
| No running or paused generation declares the path | 404 |
| Wrong method | 405 |
| Per-source rate or backlog over high water | 429 with Retry-After |
| Secret unavailable, or the append failed | 503 with Retry-After |
| Verified but malformed | 200, stored as `dead(malformed)` so the provider stops retrying |

## 9. The bus

### 9.1 Contract

```python
class Bus(Protocol):
    async def append(self, records: list[IngressRecord]) -> list[AppendResult]  # Accepted | Duplicate | Rejected(reason); durable on return
    async def next_ingress(self, router_id: str, limit: int) -> list[IngressRow]  # per-source order
    async def route(self, ingress_ids: list[int], deliveries: list[Delivery]) -> None  # atomic
    async def claim_stream(self, relay_id: str, classes: list[int]) -> StreamLease | None  # (top class, oldest pending) first
    async def pending(self, lease: StreamLease, max_items: int, max_bytes: int) -> list[Delivery]  # seq order, not held
    async def delivered(self, lease: StreamLease, through_seq: int) -> None  # after Temporal accepted
    async def hold(self, scope: Scope, reason: HoldReason) -> int  # budget | paused | window
    async def release(self, scope: Scope, reason: HoldReason) -> int
    async def settle(self, stream: StreamKey, through_seq: int, outcome: Outcome) -> None
    async def dead_letter(self, delivery_id: int, reason: str) -> None
    async def replay(self, selector: ReplaySelector) -> int  # re-route retained ingress; operator-confirmed
    async def lag(self) -> BusLag  # per class: oldest pending age, held count, relay lag
```

There are two drivers, `SqliteBus` and `PostgresBus` (`server/services/bus/sql.py`). Producers, the router, the relay and the entities never see which one is in use. A test-only `DirectBus` performs Signal-With-Start without an inbox.

### 9.2 Tables (`server/models/events.py`)

| Table | Columns (main) |
|---|---|
| `bus_ingress` | id, source_key, dedupe_key, type, class, received_at, envelope (≤ 64 KB) or body_ref, size, chainid, hop, causationid, status (`accepted` / `routed` / `no_consumer` / `dead` / `expired`), dup_count, error |
| `bus_dedupe` | (source_key, dedupe_key) UNIQUE, expires_at (dedupe window 7 days). On PostgreSQL it is kept separate from the day-partitioned `bus_ingress`, so the check spans days. |
| `bus_subscriptions` | employee, generation, trigger node or implicit kind, event types (canonical plus legacy), source scope, filter parameters (secret refs only), class, `thread_by`, lane, intake agent, merge, debounce, `on_busy`, reply-capable |
| `bus_streams` | stream_key = (workflow, generation, lane, thread), next_seq, delivered_seq, consumed_seq, top_pending_class, oldest_pending_at, hold_reason, entity_id, default_route, state |
| `bus_deliveries` | (stream_key, seq) UNIQUE, ingress_id, subscription_id, route, class, payload or ref, causal fields, status (`pending` / `held` / `delivered` / `done` / `failed` / `dead`), reason, attempts |
| `bus_leases` | name, owner, until, fencing token |
| `channel_accounts` | id, driver, external id, credential ref, status, cursor JSON, lease, last_seen_at |
| `event_blobs` | id, sha256, path, size, created_at |

**SQLite (desktop):** WAL, `synchronous=FULL` (an acknowledged event must survive power loss) and `busy_timeout=5000`. Appends are single autocommit statements. One relay task. Consumed rows are deleted in chunks after retention.

**PostgreSQL (hosted):**
- `bus_ingress` is range-partitioned by day, so retention is `DROP PARTITION`.
- Relays claim streams with `FOR UPDATE SKIP LOCKED`.
- `idle_in_transaction_session_timeout` and `statement_timeout` are set.
- Wake-ups use a post-commit kick plus a 1 s poll of a partial index. `LISTEN/NOTIFY` is used only for idle-to-pending edges, if at all, because every notifying commit serializes on one lock.

### 9.3 Router

Subscriptions are compiled from the generation snapshot in the same transaction that moves the generation to `running`. They replace the `register_trigger` Signals and the `ControlEventTypes` search attribute.

For each `accepted` ingress row, in per-source order, one transaction:
1. **Match.** Match subscriptions by type and scope, then by the plugin's `build_filter`. A filter that raises makes a `dead(filter_error)` row; it neither admits nor drops silently.
2. **Guard.** Apply the storm guards (§16.3).
3. **Number.** Lock the stream row, allocate `seq = next_seq` and write the `bus_deliveries` row. This serializes commits per stream.
4. **Mark.** Mark the ingress row `routed`, `no_consumer` or `dead`.

Fan-out is capped at 50 subscriptions per event. Re-running the router after a crash is a no-op.

### 9.4 Relay

The relay claims streams ordered by (highest pending class, oldest pending). When Temporal pushes back, it shares the Signal rate among classes by deficit round-robin with the floors of §13.1. For each claimed stream it checks, in order:
1. **The generation is active.** Otherwise the deliveries become `dead(reset)`.
2. **Budget and pause holds** (§14). `high` holds C3 and C4; `max` or a paused employee holds every class.
3. **The window.** If `delivered_seq - consumed_seq` has reached 32 inputs or 512 KB, the stream is held with reason `window`, and the backlog stays in the database (principle 4).
4. **Send.** It reads at most 16 pending deliveries or 256 KB and calls `client.start_workflow("ConversationWorkflow", id=<entity id>, start_signal="deliver", start_signal_args=[batch], id_reuse_policy=ALLOW_DUPLICATE, priority=Priority(priority_key=<class>, fairness_key=<employee id>))`.
5. **Commit.** It commits `delivered_seq` only after Temporal accepts.

**Limits:** at most one Signal-With-Start in flight per stream, and at most 4 per entity per second.

**When Temporal fails:**
- A breaker opens on unavailability, with backoff and a 5 s probe.
- RESOURCE_EXHAUSTED slows the lowest class first.
- A deterministic rejection marks the delivery dead after 5 attempts. The stream keeps moving, so there is no head-of-line blocking.

### 9.5 Delivery protocol: exactly one effect per event

1. **Ingress or driver:** append, commit, acknowledge.
2. **Router:** writes the delivery with its stream `seq`.
3. **Relay:** sends `deliver(rows, upto_seq)` and commits `delivered_seq` after Temporal accepts.
4. **Entity:** drops any row with `seq <= applied_seq`. That counter is carried across continue-as-new, so a relay crash between the accept and the commit makes only a harmless duplicate.
5. **Consumption.** For agent turns, `save_conversation` writes the transcript and advances the stream's `consumed_seq` in one transaction, so an input counts as consumed exactly when its text is in the agent's history. Graph turns without an agent settle at turn end with `settle`.
6. **Effects.** Effects carry idempotency keys (§17). The `run_runtime_mutation` ledger (`server/core/database.py:731`) covers the database effects.

### 9.6 Why the relay pushes rather than having entities pull

The alternative was a payload-free doorbell Signal with the entity fetching from the database. The relay pushes bounded batches instead, for three reasons:
- Every driver presents the same contract to the entity.
- A wake costs one action instead of three (signal, fetch, claim).
- Workflow workers never read the inbox.

This is the documented consumer pattern: Signal-With-Start, then commit after the server accepts. NAPI's property is kept: once the window fills, the relay stops pushing and the entity drains its window in one turn.

### 9.7 Alternatives rejected

| Option | Why not |
|---|---|
| Signal-With-Start straight from ingress, no inbox (RFC-0004's in-workflow mailbox) | Ingress must refuse or lose events whenever Temporal is down or throttling. No replay, no dead letters, nowhere to hold work for a budget, and every event costs 2-3 state transitions. |
| gRPC streams between services as the bus | gRPC moves events and never stores them: a crash loses whatever was buffered. Adding a log means building a broker. |
| A broker (Kafka, NATS JetStream, KurrentDB, Pub/Sub) | One more stateful service in every instance (desktops have none; Redis Streams were retired in Wave 15.3). An instance peaks at about 50 events/s, while PostgreSQL handles est. 2,000-5,000/s. |
| Dapr pub/sub | Adds a sidecar and no durability of its own. |

### 9.8 Conformance suite

`server/services/bus/conformance/` runs against both drivers:
- kill -9 at every step loses no acknowledged event;
- duplicate appends;
- FIFO per stream;
- hold and release order;
- the window;
- replay;
- 10,000 messages to one conversation cost fewer than 1,000 Signals per entity run.

## 10. Processes (the workflow types)

### 10.1 Granularity and ids

| Workflow | Id | Started by |
|---|---|---|
| EmployeeSupervisorWorkflow | `emp-<workflow_id>-g<gen>` | Start, with `REJECT_DUPLICATE` |
| ConversationWorkflow (the entity) | `conv-<workflow_id>-g<gen>-<lane>-<thread>` | the relay, by Signal-With-Start with `ALLOW_DUPLICATE` |
| Turn (MachinaWorkflow, input `event_os_version=1`) | `turn-<conv id>-<n>` | the entity: a child with `REQUEST_CANCEL` and `WAIT_CANCELLATION_COMPLETED` |
| AgentWorkflow (input `merge_version=1`) | existing child ids (`_agent_child_workflow_id`) | the turn |
| ScheduleFireWorkflow and the poll cycle | Schedule actions | Schedules `cron-…`, `poll-…`, `alarm-…` (§15) |
| ApprovalWorkflow | `approval-<id>` | approval creation |

Ids use the immutable workflow id, never the slug. Every new type:
- uses `@workflow.init`;
- has signal handlers that only buffer;
- continues as new only from the main loop, after `workflow.all_handlers_finished()`.

### 10.2 Per-type specification

| Type | State | Messages | Timers | Continue-as-new |
|---|---|---|---|---|
| Supervisor | control state and revision; alarm `next_due` and version; budget state and version; health window | **Updates** with validators:<br>• `set_control_state` (fans Stop/Resume out to the live turns in `sched_leases`; a durable Stop barrier that fixes H9);<br>• `wait_for_checkpoint`;<br>• `set_limits`;<br>• `resume_after_budget`.<br>**Signals:** `alarms_changed`, `budget_changed`, `turn_failed`.<br>**Query:** `status`. | updatable alarm timer; budget period end (midnight in the owner's time zone); 5-minute upkeep | when suggested, or daily; carries about 2 KB |
| Entity | pending window per class; `applied_seq`; offered set for the running turn; running turn handle; `turn_no`; debounce anchors; failure window | **Signals:** `deliver`, `inputs_consumed`, `turn_suspended`, `wake`.<br>**Update:** `interrupt_turn` (validator: that turn is running).<br>**Query:** `status`. | debounce (sliding plus cap); admission retry every 60 s; turn deadline per class; idle passivation; restart backoff | only between turns, when suggested or past 4,000 events or 4,000 Signals. Carries `applied_seq`, `turn_no` and at most 256 KB of pending inputs; any excess goes back to the database as `hold(window)`. |
| Turn | today's MachinaWorkflow state, plus the offered inputs | **Signal:** `offer_inputs`, forwarded to the intake agent. If that agent has already finished, the turn answers `inputs_returned`. | none new | none. Loads the graph by reference (generation plus trigger node), which fixes H10 and M6. Its result adds the consumed inputs and usage. |
| AgentWorkflow | plus offered inputs, consumed ids, merges, current class | **Signals:** `offer_inputs`, `budget_released` | none new; a turn suspended for budget waits without a timer, and an upkeep sweep fails one suspended for more than 7 days | its existing continue-as-new; the resume marker adds consumed ids, class and merges, and already carries `usage_total` (`agent_workflow.py:2997`) |
| ScheduleFire / poll cycle | none | none | none | one activity: append a tick, or poll (fetch, append and save the cursor in one transaction, then `post_emit`) |
| Approval | decision, revision, expiry | **Update:** `decide{decision, revision, key}`.<br>**Query:** `status`. | expiry, then the Undo window | one send activity, then `approval.decided` (C1) appended to the origin thread |

### 10.3 Failure semantics

- A turn that fails ends as a Failed workflow (RFC-0004 D12).
- The entity retries its inputs once, then dead-letters them with an attention item. Restart intensity is described in §18.2.
- A bug in workflow code leaves that entity or supervisor stuck and visible (`TemporalReportedProblems`). The inbox keeps accepting, and nothing acknowledged is lost (I10).

### 10.4 Versioning

- **Generation flags.** New generations get `resource_manifest.event_os_version=1` from `begin_generation` (`server/services/deployment/control.py:112`, `:160`). Older generations keep `WorkflowControlWorkflow` until they are Reset.
- **Patches.** New types need no patches. Shared types (MachinaWorkflow, AgentWorkflow) branch only on input flags frozen at admission, which no history from an older release contains.
- **Preview features** stay unused (RFC-0004 Q12): Workflow Streams, External Storage, Worker Versioning, Workflow Pause and eager start.

## 11. Merging and batching (E6)

1. **Debounce.** An idle entity that receives `deliver` waits with a sliding window and an absolute cap:

   | Class | Sliding window | Cap |
   |---|---|---|
   | Owner chat | 0.4 s | 1.5 s |
   | Customer chats | 2 s | 8 s |
   | Customer groups | 3 s | 10 s |
   | Other classes | none | none |

   `workflow.wait_condition` with a timeout drives this.
2. **Admit.** `admission.acquire(class)` (§13.3) answers granted, queued (the entity waits for `wake`), or denied by budget or pause (it also waits for `wake`).
3. **Start.** The entity takes the longest pending prefix that shares one reply route (at most 50 inputs or 256 KB) and starts turn n with references to those inputs.
4. **Offer.** When `deliver` arrives during turn n on the same route, the entity sends `offer_inputs` to the turn, which forwards it to the intake agent. The agent merges at two points:
   - **B1.** At the top of each iteration, after `_wait_until_resumed` (`server/services/temporal/agent_workflow.py:1152-1172`), it appends offered inputs as user messages before the model call.
   - **B2.** After a final answer that has not been sent (`:3078-3096`), at most twice per turn and on customer channels only (§25): it keeps the unsent draft as an assistant message, adds a one-line note that more messages arrived, and takes one more step. A customer who double-texts gets one coherent reply.

   The save at that step advances `consumed_seq`, and the agent tells the entity with `inputs_consumed`. Tool batches are never interrupted.
5. **Priority inheritance.** If a merged input has a higher class than the turn, the agent's later steps use that class's queue and Priority key, and the entity upgrades its admission lease (the PI-futex analog).
6. **Turn end.** Offered but unmerged inputs, and inputs for another route, start the next turn immediately, without debounce. The consumed inputs of a failed turn are retried once, then dead-lettered with an attention item.

**Graph runs** whose first node after the trigger is not an agent take one input per turn; `on_busy` controls overlap (`queue` by default, `skip` for cron). In a batch, the trigger output is the last input's data plus `batch: [...]`, so existing templates keep working. Subgraphs without an agent that has Context are stateless and get a stream per event, so they run in parallel.

**Passivation.** After an idle period (owner 24 h, customer 30 min, others 10 min), the entity completes. A `deliver` that lands while it is closing is not lost: the server rejects completion while new Signals are unhandled, and the relay's re-ring of an undelivered window covers the rest.

**Chat changes** (`server/services/chat/`, `docs-internal/chat_protocol.md`):
- `send_chat_message` no longer answers `run_in_progress` or `engine_unavailable`. A message sent while Temporal is down is stored and shown as queued.
- The entity is the only writer of `ChatRun`. `chat_run.open` runs at turn start, and a new `chat_run_inputs` table records merged messages.
- New run events: `run.input_merged`.
- An owner message during a running owner turn is merged at B1, or starts the next turn right away.

## 12. Conversation streams, lanes and memory (E9)

**Threads**

| `thread` | Used for |
|---|---|
| `main` | owner chat, cron, webhooks, polls and task reviews (today's continuity) |
| `t_<sha256(account, chat)[:24]>` | each customer conversation; ids carry no personal data |
| `peer_<employee>` | messages from another employee |
| origin thread | task results and approvals |
| `e_<delivery id>` | ephemeral, per event, for stateless subgraphs |

**Lanes.** Triggers whose subgraphs share an agent with Context form one lane.

**Implicit subscriptions** need no node. They are routed to the front agent: the talk agent, else the lead, else the first agent.

| Subscription | Class | Thread |
|---|---|---|
| `employee.inbox` | C3 | `peer_<sender>` |
| `employee.task_result` | C3 | origin |
| `alarm.fired` | C4 | the alarm's thread |
| `approval.decided` | C1 | origin |

**Conversation key.** It becomes (workflow, generation, agent node, thread). `agent_conversations` gains `thread_id` with default `main`, and its unique key widens to match. This is the first Alembic migration (§21.4).

**Unchanged.** RFC-0002's rules stay in force:
- requests are built from messages;
- the store records what was sent;
- loads are loud and saves are best effort;
- a carried rollover transcript beats the stored one.

## 13. Scheduler (E10)

### 13.1 Classes

| Class | Sources | Priority key | LLM queue | Floor | Turn deadline |
|---|---|---|---|---|---|
| C1 owner | owner chat, approval decisions | 1 | `llm-c1` | 30%, plus one reserved slot per employee | 15 min |
| C2 customer | WhatsApp, WhatsApp Business, Telegram, Discord, email replies | 2 | `llm-c2` | 40% | 15 min |
| C3 internal | employee messages, delegations, task results, signed provider webhooks | 3 | `llm-c3` | 15% | 30 min |
| C4 background | alarms, cron, polls, watched data, generic webhooks, Start ticks | 4 | `llm-c4` | 15% | 60 min |
| C5 upkeep | digests, sweeps | 5 | `machina-tasks` | none | n/a |

A trigger may override its class. A class's unused share flows down the order.

### 13.2 Temporal mapping

- **Priority keys** are set on entity starts, turn children and every activity (inherited), and on sends.
- **Fairness key = employee id**, with weight 1. Each instance has one owner, so there are no tenant keys.
- **The dev server** gets `--dynamic-config-value matching.enableFairness=true`. Priority is on by default (`matching.useNewMatcher`).
- **Both need the pinned server** at version 1.31 or later (RFC-0004 D1). Without them behaviour stays correct, only unfair.
- **Task queues.**
  - Workflow tasks share `machina-tasks`.
  - LLM steps run on the four class queues and leave `machina-tasks` (RFC-0004 M9). Each run freezes its queue in its input.
  - Tools stay on their plugin queues with Priority keys; sends go to `messaging`.
- **Slot pool.** Each worker process runs one Worker per class queue. They share one `ClassSlotPool` built on `temporalio.worker.CustomSlotSupplier` (stable in SDK 1.30, `temporalio/worker/_tuning.py:160`). The pool:
  - guarantees each class its floor;
  - grants freed slots in strict class order;
  - limits idle poller reservations to 1-2 per class, because `reserve_slot` runs before polling.

  Fallback: fixed pool sizes. The existing `WorkerTuner.create_composite` usage in `worker.py` shows where it plugs in.
- **Starvation shows.** Background queues get `schedule_to_start` timeouts.

### 13.3 Admission

Tables:
- `sched_leases`: turn, employee, class, state `running` or `suspended`, control revision. It is also the live-turn registry used by Stop and Reset.
- `sched_waiters`.

`acquire` is one transaction:
1. Budget and pause gate.
2. Per-employee cap: 4 by default, plus the reserved owner slot.
3. Grant if the class is below its floor. Grant from shared capacity only if no higher class is waiting. Otherwise queue the request.

`release` serves waiters in this order:
- classes below their floor;
- strict class order;
- round-robin by employee within a class.

It then wakes the waiting entity with `wake`. The supervisor's upkeep reaps leases whose turn is confirmed closed.

### 13.4 Provider depth and priority inversion

- **Concurrency.** Worker slots cap concurrent LLM activities. Per-credential token buckets cap requests and tokens per minute for each class. Retry-After responses become BENIGN retries with `next_retry_delay`.
- **Priority inversion is avoided by:**
  - floors;
  - priority inheritance on merge (§11);
  - no lock held across turns;
  - the owner's **Interrupt** (`interrupt_turn`): when a lower-class turn holds the same conversation, it is cancelled at its next merge point and its inputs return to pending.

## 14. Budgets (E11)

**Ledger:**
- `employee_budgets`: daily limit, soft ratio 0.8, the owner's time zone, revision.
- `employee_spend`: day, reserved, settled, state (`normal` / `high` / `max`), version.
- `spend_events`: key `{workflow_id}:{activity_id}`, UNIQUE.

The OpenCompany Gateway already records spend per call and replays retried calls at no cost. The ledger also covers calls made with the owner's own keys.

| Where | Rule |
|---|---|
| Relay, before delivery | Reads the cached state (refreshed by transition events, 30 s TTL). `high` holds C3 and C4; `max` or paused holds every class. |
| Entity, before each turn | `admission.acquire` reads the ledger, which is authoritative. |
| LLM step (`agent.execute_llm_step`) | `budget.reserve(key, upper_estimate)` (input tokens times price, plus `max_tokens` times output price), then `settle(key, actual)` after the call. If the reservation would pass 100%, it raises `BudgetExceeded`: non-retryable and BENIGN. The agent suspends, it does not fail. A C3 or C4 turn that sees `high` suspends at its next step and holds no slot. |

**Transitions:**
- The transaction that crosses a threshold appends `employee.budget.changed` to the supervisor.
- At `max`, the supervisor runs Stop with `pause_reason="budget"`, through the existing automatic-pause path (`_automatic_pause_values`, `server/services/deployment/handlers.py:1030`). The owner gets an attention item and a notice with **Resume**.
- At midnight in the owner's time zone, the supervisor's period timer resets the day and resumes, with a notice.
- `resume_after_budget` (raise or top up the limit) does the same at once.
- Held inputs are released in (class, seq) order, and suspended turns get `budget_released`.

**Continue-as-new and retries.** Enforcement uses only the ledger. Reservation keys are activity ids, and iteration numbers continue across continue-as-new, so retries never double-count.

**Overshoot** is bounded by price-table error, because reservations use the upper estimate.

## 15. Timers (E5)

- **Cron.**
  - Schedule `cron-<workflow_id>-g<gen>-<node_id>`, built from immutable node ids (fixes RFC-0004 M2).
  - Its action is ScheduleFireWorkflow, which appends a C4 tick, so ticks obey class floors and the 80% gate.
  - Overlap `BUFFER_ONE` covers the tiny fire workflow. Jitter is 60 s, and catch-up is one interval, at most 1 hour.
  - The business overlap rule is the entity's `on_busy`, default `skip` (fixes H6).
  - Stop pauses Schedules by id and Reset deletes them by id. Re-arm recreates them from the snapshot (I10).
- **Polls.**
  - Schedule `poll-<workflow_id>-g<gen>-<node_id>` with overlap `SKIP`.
  - The poll cycle fetches, appends new items and saves the cursor in one transaction, then runs `post_emit` (fixes M3).
- **Alarms (self-scheduled wake-ups).**
  - Tools `set_alarm`, `cancel_alarm` and `list_alarms` write `alarms`:
    - `id = hash(turn, tool_call_id)`, thread, `due_at` (UTC);
    - `if_late`: fire with a note, or `skip_after:<duration>`;
    - `cancel_on: inbound`, repeat, status.
  - They then signal `alarms_changed`.
  - The supervisor keeps one updatable timer for the earliest due alarm. `alarms.fire_due` moves the alarm to `fired` by compare-and-swap and appends an input stamped with `scheduled_for`, `fired_at` and its lateness.
  - Recurring alarms become Schedules `alarm-<id>`. Limit: 500 per employee.
- **Follow-ups** are alarms with `cancel_on=inbound`; the router cancels them in the same transaction as the inbound message.
- **The Start node** fires once per generation (dedupe key `start:<gen>`; Phase 0.4).
- **Approval expiry** is the ApprovalWorkflow's timer. `expire_due` stays as a backstop.

## 16. Employee messaging and watched data (E7, E8)

### 16.1 Messages and delegation

- **Tools** (`server/nodes/tool/employee_mail/`): `send_to_employee`, `delegate_to_employee` and `complete_task`. They write `ipc_tasks` (task id, requester employee, generation and thread, assignee, state, deadline, `reply_to`) and append to the inbox in the sender's own transaction, as an outbox.
- **Scope.** Any employee in the instance may address any other.
- **Routing.**
  - The target's implicit `employee.inbox` subscription puts the message in thread `peer_<sender>`.
  - Results route through the explicit `reply_to`, never a broadcast. The router accepts a completion only from the assignee (`assigned` to `completed`, by compare-and-swap). A deadline alarm delivers `timed_out`.
  - Team tasks inside one employee become scoped inputs to their origin thread (fixes H11).

### 16.2 Watched shared data

- The Context, Canvas and Memory stores emit `com.opencompany.data.<store>.<entity>.changed` (subject = the record id) from their write paths. Each event carries the writing turn's causal fields.
- Subscriptions add match rules on store and subject.
- Inputs coalesce per (subscription, record) and are delivered level-triggered: "changed N times; re-read the record". Up to 200 subjects, then a single `resync` input.

### 16.3 Storm guards (`server/services/bus/causal.py`, `token_buckets`)

- At most 8 hops.
- **Cycle check.** An input whose target is already on its `path` (at most 16 entries) is suppressed as `loop_suppressed`.
- No self-wake from an employee's own writes.
- A token bucket per edge: 10 per minute, burst 20.
- **Breaker.** After 3 exhausted windows the breaker opens: the edge becomes a 15-minute digest and an attention item names both employees. It half-opens after 1 hour.
- **Merged turns.** A merged turn's outputs carry the maximum hop and the union of the input paths.

## 17. Egress

### 17.1 Outbox

`outbound_messages` holds:
- the idempotency key (UNIQUE), employee, thread, turn, account, recipient (encrypted), kind, class;
- body or ref;
- status: `held`, `queued`, `sending`, `sent`, `failed`, `unknown` or `expired`;
- approval id, `not_before`, claim token, provider message id, receipt.

The reply key is a hash of (thread, route, consumed input set, node). A retried turn that answers the same inputs is therefore deduplicated even when its text differs.

### 17.2 Sending

`egress.send` runs on `messaging` with Priority = class and fairness key = channel account:
1. Claim the row by compare-and-swap.
2. Take a token from the per-account and per-recipient bucket. If none is free, raise a retryable error with `next_retry_delay`, so no worker slot is held while waiting.
3. Call the provider with its idempotency key where it has one.

### 17.3 Uncertain outcomes (E12)

For a provider without idempotency keys, a retry that finds an expired `sending` claim marks the row `unknown`. That raises an attention item and offers a Retry button. It never resends on its own.

### 17.4 Order and backpressure

- Sends are awaited inside turns, so each thread stays FIFO.
- Above high water the producing turn waits (BQL, writeback throttling).
- C4 sends may use only the top half of a bucket.

### 17.5 Approvals

- A send that needs approval is `held`.
- ApprovalWorkflow (an Update `decide` plus a durable timer, RFC-0004 D13) sets `not_before` to the end of the Undo window and sends once. It replaces `ApprovedToolCallWorkflow`.
- Approved sends go out in the order the owner decides them.

### 17.6 Receipts and UI

- Provider status events update the row, and a failure becomes an input to the thread.
- `StatusBroadcaster.broadcast` requires an audience: owner, workflow or system. Frames that carry content need an owner or workflow audience, so content never reaches every socket.
- Chat streaming through the chat hub is unchanged.

## 18. Supervision, observability and telemetry

### 18.1 Watchdogs

- Turn deadlines per class, owned by the entity.
- A stuck-turn alarm when there has been no progress for 10 minutes.
- Router stalled: the oldest `accepted` row is older than 30 s.
- A window full while its entity is idle (a credit leak).
- `TemporalReportedProblems`.
- The chat watchdog reads turns by known id, never Visibility.

### 18.2 Restart intensity (OTP MaxR/MaxT)

- **Per thread.** 3 failed turns in 10 minutes back off 1, 5, then 30 minutes. 5 in an hour quarantine the thread, with its inputs held.
- **Per employee.** 30% of turns failing (at least 10 in 15 minutes), or 3 quarantined threads, makes the employee `degraded`: C3 and C4 pause and the owner is told.

### 18.3 Attention items

`attention_items` (kind, severity, ref, first and last seen, count, state) appear in the employee summary. The kinds are:
- quarantined thread, dead input, unknown or failed send;
- budget high, budget max;
- driver down, Temporal problem, stuck turn;
- loop suppressed or breaker open, missed alarm, expired approval.

### 18.4 Metrics (measure waiting)

| Area | Metrics |
|---|---|
| Ingress | append latency |
| Bus | relay lag; oldest pending age and held count per class |
| Scheduling | admission wait and slot wait per class; `schedule_to_start` per queue |
| Temporal | Signals and history size per run |
| Turns | duration and outcome |
| Egress | depth and latency |
| Health | dead letters by reason, open breakers, budget transitions, event-loop lag |

A per-employee `employee_timeline` view joins ingress, deliveries, turns, outbox, alarms, messages and attention on `chainid`.

### 18.5 Fleet telemetry (E14)

- **Hosted instances export** metrics and error reports over OpenTelemetry (OTLP) to a central collector. Error reports carry the exception type, version, count and traceback frames without local values.
- **Never exported:** message text, prompts, customer data and credentials.
- **Enforcement.** A content-filter test checks every exported attribute name and value type.
- **Desktop.** The desktop app exports nothing unless the owner opts in.

## 19. Delivery semantics, ordering and overflow

**Delivery at each stage**

| Stage | Semantics |
|---|---|
| Provider to ingress | at-least-once |
| Acceptance | exactly once within the 7-day dedupe window |
| Routing | exactly once |
| Relay delivery | at-least-once, deduplicated by `seq` |
| Consumption | exactly once (transcript and cursor in one transaction) |
| Processing | at-least-once, with one retry |
| Effects | effectively once through idempotency keys; at-most-once with `unknown` where a provider has none |

**Ordering**
- FIFO per stream and reply route.
- Across routes in one stream: sequence order at turn boundaries, but a running turn may absorb later inputs on its own route first.
- No order across streams.
- Sends are FIFO per thread.

**Overflow (defaults)**

| Source | Bound | When full | Staleness |
|---|---|---|---|
| Owner chat | 100 pending per session | `queue_full` at send; nothing saved | never expires |
| Customer chats | entity window 32; 5,000 per account | never dropped; above high water the driver stops reading (Telegram, bridge) or answers 429 (WhatsApp Business) | delivered late with a lateness note; WhatsApp Business needs a template after 24 h |
| Signed webhooks | 10,000 unrouted per endpoint; 50/s | 429 | none |
| Generic webhooks | 1,000 unrouted; 10/s | 429 | none |
| Polls | 1,000 unrouted | cycle skipped; cursor stays | n/a |
| Cron | 1 pending tick per Schedule | the newest wins; `on_busy` | older than one interval: `expired` |
| Alarms, open tasks | 500 alarms, 200 open tasks per employee | creation refused | `if_late`; task deadline |
| Employee messages | 10 per minute per edge; 500 pending per target | breaker, then digest | 24 h TTL, then `expired` |
| Watched data | 1 per (subscription, record); 200 subjects | coalesce, then `resync` | level-triggered |
| Receipts, typing, presence | 1,000 in memory per account | drop oldest | 30 s |

## 20. Instance tiers (E1-E3)

| | Desktop | Hosted tier S | Hosted tier L |
|---|---|---|---|
| Employees | any, warning above about 100 | up to about 100 | up to about 1,000 |
| App database and inbox | SQLite | PostgreSQL 16 | PostgreSQL 16 |
| Temporal | CLI dev server on SQLite | CLI dev server on SQLite | open-source `temporal-server` on the instance's PostgreSQL (databases `temporal` and `temporal_visibility`), 64 history shards fixed at creation |
| Processes | one | API with relay and workers | API with relay; a workflow worker; activity workers per class |
| Peak load (est.) | below tier S | about 5 events/s, 50 Temporal actions/s, 65 LLM calls in flight | about 50 events/s, 500 actions/s, 650 LLM calls in flight |
| Limit | dev server est. 100-500 actions/s on one shard | same | 64 shards; app database after the advisory-lock fix (N8) |

The peaks are scaled from a 20-employee model: 2,600 turns/day, about 35k Temporal actions/day and 8-10 actions/s at peak. At tier L the owner's LLM provider limits bind before Temporal does, and the class floors decide who waits.

**Tier S hardening.** The CLI documentation warns that the dev server "is not intended for production use" and skips some HTTP security checks. Tier S therefore:
- pins the CLI version and checksum (RFC-0004 D1);
- persists to `--db-filename` with the WAL pragmas already set (`_runtime.py:153-158`);
- binds to 127.0.0.1 only;
- sets the namespace retention to 7 days;
- passes the dynamic config for priority and fairness;
- keeps its database in the instance backups.

If its database is lost, the inbox and I10 bring the instance back without losing an acknowledged event.

**Tier L setup**, all in the deploy logic:
- The single-process `temporal-server` runs all services.
- Its schema is created and upgraded with `temporal-sql-tool`.
- It binds to localhost.
- It is upgraded one minor version at a time, as the production checklist requires.
- The shard count is fixed when the cluster is created, so 64 is confirmed by the tier-L load test before the first tier-L instance exists.

**Moving from S to L** runs in the maintenance window, and only once every generation runs on the new path (Phase 4 or later):
1. Stop every generation at its checkpoint. Inputs keep arriving in the inbox.
2. Wait for running turns. Turns past their deadline are cancelled and their inputs return to pending.
3. Start the full server, and create the namespace and search attributes.
4. Re-arm from the app database: supervisors, Schedules and alarms come back. Then Resume, and the inbox drains.

Temporal histories are not migrated (I10). The old `temporal.db` is kept for 7 days.

## 21. Fleet operations (E13, E14)

### 21.1 Deploy logic

Every instance change lives in the deploy logic, never in a hand-configured VM: `cli/terraform/gcp/`, `cli/terraform/aws/`, the startup scripts, `install.sh` and `docker/`. Hosted instances:
- install PostgreSQL 16 and create the app and Temporal databases;
- pin the OpenCompany and Temporal versions;
- choose their tier;
- configure the telemetry exporter.

**Today's behaviour that must change.** The GCP startup script reinstalls the package on every boot, and the installer service follows npm latest within minutes of a publish. Hosted instances must instead stay on their pinned version until the next window.

### 21.2 Weekly window

All hosted instances upgrade together in one fixed weekly UTC window (default: Sunday 03:00).

**Before the window:**
- The release must pass the replay gate, the golden identity test and the bus conformance suite.
- Our own staging instances run it from 3 days earlier.

**During the window, each instance:**
1. snapshots its databases;
2. applies forward-only migrations;
3. restarts;
4. checks `/health/ready`;
5. if the check fails, rolls back to the previous version and snapshot on its own, and reports the outcome through telemetry.

**Temporal** moves at most one minor version per window. Inputs arriving during the restart wait at their providers (webhook retries, the Telegram backlog, the bridge's outbox) and land in the inbox afterwards.

### 21.3 Backups and restore

- **Daily backups.** A PostgreSQL dump with WAL archive, plus a snapshot of the data directory, go to the instance's bucket.
- **Restore** always runs the reconcile:
  - deliveries `delivered` but not consumed return to pending;
  - Schedules and alarms are re-armed;
  - orphaned chat runs fail.
- **No duplicate sends.** Send keys come from workflow and activity ids, so in-flight activities that run again send nothing twice.

### 21.4 Schema migrations

- Alembic manages both databases with expand-then-contract changes: a release only adds, and removals ship two releases later.
- This replaces the ad-hoc `_migrate_*` startup steps and `_validate_shared_schema` (N10).
- The first migration adds `agent_conversations.thread_id`. Until Alembic lands (Phase 9b), Phase 0.6's index change uses an explicit startup step under the existing advisory lock.

## 22. Scenarios

**(a) Three WhatsApp messages in 2 s while turn 7 is replying to message 41**
1. The bridge streams each event over `Publish`.
2. The bus appends each one (key `whatsapp:A:C:<id>`) and acks.
3. The bridge then acks WhatsApp and deletes its outbox row.
4. The router writes deliveries 42, 43 and 44 to stream T as C2.
5. The relay sends `deliver[42..44]` to `conv-W-g7-L-T` and commits `delivered_seq`.
6. Turn 7 is running on the same route, so the entity offers them; the turn forwards them to the agent.
7. The agent's step 2 returns a final answer. At B2 it keeps the unsent draft, appends the three messages and takes step 3, which writes one reply covering all four.
8. The save advances `consumed_seq` to 44.
9. The send step writes an `outbound_messages` row keyed over inputs 41-44, takes a token for account A and recipient C, and sends.

If the employee had been idle, one debounced turn takes 42-44 at 3.6 s. If the reply had already gone out, 42-44 start turn 8 immediately.

**(b) A Stripe webhook delivered twice, with a restart in between**
1. Delivery 1: size check, signature verified over the raw bytes, appended with key `evt_X`, answered 200.
2. The backend crashes. On boot, the router resumes from `accepted` rows and the relay from pending deliveries.
3. If the crash came after Temporal accepted the Signal but before `delivered_seq` was committed, the entity drops the repeat by `seq`.
4. Delivery 2 has the same `evt_X`: it is a duplicate, gets 200 and routes nothing.
5. One turn handles the event. Its Stripe calls and emails carry keys derived from (input, node).

If the crash came before the append, Stripe never saw a 2xx, and its retry becomes the first append.

**(c) The owner chats while the same employee's cron job runs**
1. The 08:00 Schedule appends a tick (C4, thread `main` of the worker lane), which runs on `llm-c4` with priority 4.
2. At 08:03 the owner's message (message plus ingress row in one transaction) goes to the talk lane as C1.
3. After the 0.4 s debounce, admission grants the employee's reserved owner slot, and the turn runs on `llm-c1`. The slot pool serves C1 first.
4. The two turns use different conversation keys and run side by side.

If an editor graph wires both triggers into one agent and one thread, the owner's message waits as "next", and the owner can press Interrupt.

**(d) A reminder for tomorrow 09:00, with the laptop asleep at 09:00**
1. `set_alarm` writes the row and signals the supervisor, which sets its timer.
2. On wake at 09:47 the dev server fires the overdue timer. `alarms.fire_due` appends an input that is 47 minutes late.
3. Signal-With-Start restarts the passivated entity.
4. The agent reads "Reminder due 09:00, firing 47 min late".

With `skip_after:2h`, a later wake marks the alarm `missed` and raises an attention item instead.

**(e) Employee A delegates to B, and B's result wakes A**
1. `delegate_to_employee` checks hops, the edge bucket and the open-task cap, then writes `ipc_tasks` and appends. A's turn ends with "assigned".
2. B's inbox puts the task in thread `peer_A` as C3, and B runs.
3. `complete_task` (assignee only, by compare-and-swap) appends a result with `reply_to = (A, T_A)`.
4. The router puts it in T_A. A's next turn uses the same conversation key, so A remembers its plan.

If the deadline passes first, A gets `timed_out`.

**(f) A CRM record changes, waking two employees, and one would wake the other**
1. Chain c0 creates inputs for E1 and E2 at hop 1 (C4).
2. E1 writes the record: hop 2, path [E1]. E1 itself is not woken. E2's input is coalesced, or merged into its running turn.
3. E2 writes the record: hop 3, path [E1, E2]. The input for E1 is a cycle, so it is suppressed.
4. If the ping-pong continues through new chains, the edge bucket runs out, the breaker turns the edge into a digest, and an attention item names E1 and E2.

**(g) The dev server is down for 10 minutes while Telegram messages and webhooks keep arriving**
1. Drivers, ingress and the router depend only on the database.
2. Telegram's offset advances after each append, webhooks get 200, and owner messages show as queued.
3. The relay's breaker probes every 5 s.
4. Backlogs past high water pause Telegram reads (Telegram keeps them for 24 h) or answer 429.
5. On recovery the relay drains by class floors, owner first. Alarms and catch-up ticks fire late and coalesce.

Nothing acknowledged is lost.

**(h) A 5 MB webhook body**
1. `Content-Length` is above the 256 KiB default cap, so the answer is 413 before the body is read.
2. If the endpoint declares 10 MiB, the body is streamed to `event_blobs` with an incremental HMAC. The row keeps `body_ref`, a 4 KB preview and the allowlisted headers.
3. Turns carry only the reference, and nodes read it through a FileRef.

No Temporal payload can exceed a limit (C1), and the body is never broadcast.

**(i) The budget crosses 80% and then 100% (limit $20 a day)**
1. A C2 step reserves $0.20 at $15.90 spent, reaching 80.5%. The ledger moves to `high`.
2. The relay holds the employee's C3 and C4 inputs, and the admission for a cron turn is denied. A running C4 turn suspends at its next step. C1 and C2 continue.
3. A reservation that would pass $20 raises `BudgetExceeded`, and that turn suspends.
4. The ledger moves to `max`. The supervisor stops the employee with `pause_reason=budget`, and the owner gets a notice with Resume.
5. The owner adds $10, or midnight arrives. The state returns to `normal`, held inputs are released in (class, seq) order, and suspended turns get `budget_released`.

## 23. Failure modes

| Failure | Detected by | Effect | Recovery |
|---|---|---|---|
| Crash before the append | the provider saw no 2xx | none | the provider retries |
| Crash after the append, before routing | age of the oldest `accepted` row | delay | the router resumes |
| Crash after Temporal accepted, before `delivered_seq` | none needed | duplicate delivery | the entity drops it by `seq` |
| Relay dies holding a stream lease | lease expiry | delay | another relay task claims it; the fencing token stops stale updates |
| Temporal (dev server) down or restarting | relay breaker | backlog, 2xx continues | probe, then drain by class floors |
| `temporal.db` lost or corrupt | server fails to start | in-flight turns lost | rebuild: start empty, re-arm from the app database (I10); unconsumed inputs re-run |
| App database unavailable | ingress errors | 503; drivers stop reading | providers retry; Telegram holds 24 h; the bridge's outbox grows |
| `RESOURCE_EXHAUSTED` from Temporal | relay error rate | slower delivery | lowest class slowed first; backoff |
| Window leak | window full while the entity is idle | stream held | the entity reports consumption when idle; upkeep resets |
| Entity or supervisor bug | `TemporalReportedProblems` | that thread or employee stuck | fix, then replay; attention item |
| Turn fails | child failure | inputs `failed` | retried once, then dead-lettered; restart intensity |
| Turn hangs | deadline timer; progress watchdog | slot held | the turn is cancelled, then the retry policy |
| Poison input | repeated failures | thread quarantined | input dead-lettered; the thread continues |
| Continue-as-new carry too large | size check before continue-as-new | none | excess returned to the database |
| Signal or Update limits near | counters | none | earlier continue-as-new; relay batching |
| Two readers on one account | lease fencing | duplicate ingest | dedupe |
| Loop between employees | path, hop, bucket | suppressed or digest | breaker half-opens after 1 hour |
| Budget overshoot | ledger | bounded by price error | pause |
| Missed Schedule while asleep | lateness metrics | late or coalesced ticks | catch-up window, `on_busy`, `if_late` |
| Provider outage or 429 | step retries (BENIGN) | slower turns | Retry-After; spend is not double-counted |
| Unknown send outcome | expired claim | `unknown` | the owner's Retry (E12) |
| Reset while work is in flight | generation fence | old entities cancelled; inputs `dead(reset)` | none needed |
| A weekly upgrade fails on an instance | readiness check | instance down briefly | automatic rollback to the previous version and snapshot |

## 24. Rollout

### 24.1 Rules

1. **Durable before fast.** Every phase leaves an append-only record, so a rollback never drops an acknowledged event.
2. **Versioned per generation.** New behaviour is a `resource_manifest` flag frozen into workflow input. `workflow.patched` is used only inside shared types.
3. **One path per generation.** Each generation uses exactly one event path, never a choice per event, so no conversation gets two replies during cutover.
4. **Replay gate.** No change to workflow code merges until histories recorded on the previous release replay green.
5. **Expand-only schema.** Additive migrations only.

### 24.2 Phases

| Phase | Content |
|---|---|
| 0 | Nine independent fixes without workflow-command changes:<br>• 0.1 webhook route table, 401/404/405 and empty path (C3);<br>• 0.2 scoped task events (H11);<br>• 0.3 body cap and the undrained queue (C1, N11);<br>• 0.4 Start once per generation (C4);<br>• 0.5 Telegram backlog (N1);<br>• 0.6 PostgreSQL chat lane (N6);<br>• 0.7 Discord public ingress (N4);<br>• 0.8 Twitter receive (N5);<br>• 0.9 broadcast audience (N7). |
| 1 | Safety net:<br>• fixture recorder and replay gate;<br>• golden identity test;<br>• pinned Temporal CLI (RFC-0004 D1);<br>• `TEMPORAL_TEST_CLI` in CI. |
| 2 | Durable ingress and router delivering to today's controllers by known id (H1, N3, N9):<br>• WAL on `workflow.db`;<br>• bus tables and both drivers;<br>• rollout per source behind `EVENT_INGRESS_MODE`, after a shadow soak that compares Visibility targets with registry targets. |
| 3 | Drivers with durable acks (N1, N2, M3):<br>• registry and leases;<br>• Telegram offsets;<br>• the edgymeow gRPC protocol with outbox and `SynchronousAck` (cross-repo);<br>• Discord catch-up;<br>• polls as Schedules;<br>• send idempotency keys. |
| 4 | Entities for new generations (`event_os_version=1`):<br>• supervisor, conversation entity, turn mode, merge points;<br>• chat as the entity's single writer;<br>• 4b: customer channels and per-thread histories. |
| 5 | Scheduler and budgets:<br>• class queues, slot pool, Priority and fairness;<br>• admission;<br>• daily ledger, holds, pause, midnight resume. |
| 6 | Timers and messaging:<br>• cron Schedules by immutable ids with `on_busy` (H6, M2);<br>• alarms and follow-ups;<br>• employee messaging;<br>• watched data;<br>• storm guards. |
| 7 | Egress and approvals:<br>• outbox, buckets, `unknown`;<br>• ApprovalWorkflow;<br>• audience-scoped broadcasts. |
| 8 | Supervision:<br>• restart intensity, quarantine, degraded mode;<br>• attention items;<br>• metrics, timeline. |
| 9 | Instance tiers and fleet:<br>• 9a: pinned versions, the weekly-window upgrader with rollback, telemetry, staging, backups;<br>• 9b: PostgreSQL in hosted instances, Alembic, the advisory-lock replacement (N8, N10);<br>• 9c: tier L server, the S-to-L move, load tests. |
| 10 | Retirement:<br>• R_k: new generations default to the new path;<br>• R_k+1: old generations are upgraded at boot (drain at the Stop checkpoint, then Reset and Start with the same snapshot and conversation store, with a notice);<br>• R_k+2: remove Visibility emit, the poll-coroutine registry and the legacy listener registrations once none is open. |

**Critical path:** 0, then 1, 2, 4, 5, 6 and 10.

**Parallel lanes:**
- Phase 3 after Phase 2.
- Controller hardening for old generations after Phase 1, serialized with any other controller edit:
  - a size guard on child input (C1);
  - poll loops re-raise cancellation (H8);
  - a slim continue-as-new carry (H10).
- Phases 7 and 8 after Phase 4.
- 9a after Phase 1; 9b before the first hosted PostgreSQL instance; 9c after Phase 5.

### 24.3 Patch markers

A marker becomes `deprecate_patch` one release after no open history can lack it. That only becomes true through Phase 10's upgrade or a periodic rollover. It is removed one release later. No marker is ever deleted in one step.

### 24.4 Progress

**Phase 0 landed on 2026-10-23**, one commit per item. Where an item differs from §24.2, the difference is noted.

| Item | Commit | Notes |
|---|---|---|
| 0.1 Webhook routing and auth | db55d4e9 | Route table in `nodes/trigger/webhook_trigger/_routes.py`. The expected header value is read from the saved node parameters. It is blocked from snapshots, Temporal input and workflow export. |
| 0.2 Scoped task events | daddf460 | The broadcast frame is an identity-only envelope. |
| 0.3 Body cap | 1ebfd5d9 | `WEBHOOK_MAX_BODY_BYTES` lives in `.env.template` and is read through `core.env_defaults`. Poll events are not capped yet; that moves to Phase 3. |
| 0.4 Start once per generation | b4656c22 | A runtime-mutation ledger claim alone; no `rearm` flag, no deterministic run id. |
| 0.5 Telegram backlog | 40c73daf | No age limit, because customer messages are never dropped (E11 overflow policy). |
| 0.6 PostgreSQL chat lane | fff57583 | |
| 0.7 Discord public ingress | 2f9a2aa8 | New registry `register_public_ingress_path`. |
| 0.8 Twitter receive | edf29a76 | Refused at Start and on a canvas Run rather than ported. A port needs paid X API access to test. |
| 0.9 Broadcast audience | 7745ca14 | `emit`'s `broadcast` has no default. No client read any channel or provider frame, so those are no longer broadcast, and no audience-scoped broadcast was needed. |

## 25. Defaults chosen and open items

### 25.1 Defaults (change at review)

- **B2 late merge** applies to customer channels only. In owner chat, a new message starts the next turn right away, so no streamed draft is ever replaced.
- **Class floors** are 30/40/15/15, plus one reserved owner slot per employee.
- **Webhook classes.** Signed provider webhooks are C3; generic webhooks and polls are C4; any trigger can override.
- **Debounce windows, passivation times, overflow bounds and storm limits** are as given above.
- **Retention:**
  - ingress bodies: 7 days;
  - deliveries and the timeline: 30 days;
  - dead letters: 90 days;
  - Temporal history: 7 days.
- **Late alarms** fire with a lateness note; `skip_after` is optional per alarm.
- **Messages that providers held during downtime** are delivered with a lateness note.
- **Generic webhook dedupe** uses a configured header, otherwise a body hash within 10 minutes.
- **The maintenance window** is Sunday 03:00 UTC; staging instances upgrade 3 days earlier.
- **Tier L** uses 64 history shards, confirmed by its load test.

### 25.2 Still open

- **History encryption** with a payload codec on hosted instances (RFC-0004 Q13).
- **Temporal retries for graph nodes** marked safe (RFC-0004 Q9).
- **Startup verification of open runs**: refuse, or start and mark "needs Reset" (RFC-0004 Q15).
- **When old generations are upgraded automatically** (§24.2, Phase 10), and what the owner is told.

## 26. Acceptance gates

| Gate | Check |
|---|---|
| EG1 | Under kill -9 at any step, no acknowledged event is lost (conformance suite on both drivers) |
| EG2 | Turning a flag back off drains the inbox; nothing already appended is dropped |
| EG3 | One user's socket never receives another user's content; webhook and provider payloads are never broadcast |
| EG4 | A second chat run in one session succeeds on PostgreSQL |
| EG5 | Five messages within the debounce window produce one turn; a message sent mid-turn appears in the next step's model input; tool batches are never split |
| EG6 | A generation paused on the previous release resumes after every phase (pinned CLI, end to end) |
| EG7 | 10,000 messages to one conversation cost fewer than 1,000 Signals per entity run, and its history stays under 10 MB |
| EG8 | SQLite append p99 is under 25 ms at 50 events/s |
| EG9 | Webhooks answer 401, 404, 405 and 413 as specified; 2xx only after commit; 503 when the append fails |
| EG10 | Restarting the backend twice starts no second Start-node run (RFC-0004 AG17) |
| EG11 | Budget: past 80% background inputs are held while chats run; at 100% the employee pauses with a notice; at midnight it resumes |
| EG12 | An event for a generation that started 10 ms earlier is delivered (RFC-0004 AG12) |
| EG13 | A Temporal database replaced by an empty one loses no acknowledged event; re-arm restores generations, Schedules and alarms |
| EG14 | Tier S sustains 100 employees on the dev server; tier L sustains 1,000 employees at about 500 actions/s |
| EG15 | The telemetry export contains no message text, prompts, customer data or credentials |
| EG16 | A weekly-window upgrade with a forced readiness failure rolls the instance back by itself |
| EG17 | Two customers' conversations never share an agent history |
| EG18 | An employee-to-employee loop is cut at 8 hops, and a hot edge becomes a digest with an attention item |

## 27. What this RFC does not change

- The plugin model (`BaseNode`, the registries, NodeSpec as the source of truth).
- The WebSocket protocol envelope and the CloudEvents `WorkflowEvent`.
- The chat wire contract, except the additions in §11.
- RFC-0002's store semantics, except the thread in its key.
- Canvas Runs: `event_waiter` stays for manual Runs, and the relay notifies local waiters.
- The cooperative pause design (Temporal's native Workflow Pause stays unused).
- The decision to leave workflow execution and run timeouts unset.
- The provider-neutral agent loop.

## 28. Docs to update with each phase

| Document | Change |
|---|---|
| `docs-internal/event_framework.md` | Inbox, router and relay replace Visibility routing; drivers; subscriptions; storm guards |
| `docs-internal/chat_protocol.md` | Merged messages, no `run_in_progress` or `engine_unavailable`, `run.input_merged`, queued while Temporal is down |
| `docs-internal/temporal-workflow-control.md` | Supervisor, entities, Start once per generation, re-arm and I10 |
| `docs-internal/TEMPORAL_ARCHITECTURE.md` | New workflow types, class queues, slot pool, Priority and fairness, tiers |
| `docs-internal/discord_service.md` | Public ingress prefix; append before the deferred reply |
| `docs-internal/docker.md`, deploy runbooks | PostgreSQL per hosted instance, tiers, weekly window, telemetry, backups |
| `CLAUDE.md` | The rows on the event framework, chat protocol, Temporal architecture and Docker |
| `.env.template` | `WEBHOOK_MAX_BODY_BYTES`, `EVENT_INGRESS_MODE`, `EVENT_OS`, the telemetry endpoint, the maintenance window |

## 29. References

- **Temporal:**
  - Python SDK guide: https://docs.temporal.io/develop/python (message passing, continue-as-new, child workflows, schedules, workers, testing).
  - Task queue priority and fairness: https://docs.temporal.io/develop/task-queue-priority-fairness
  - Design patterns (entity workflow, event accumulator, signal-with-start, approval, fairness): https://docs.temporal.io/design-patterns
  - Limits: https://docs.temporal.io/workflow-execution/limits
  - Multi-tenant patterns, the CLI server reference (https://docs.temporal.io/cli/command-reference/server), the upgrade guide (https://docs.temporal.io/self-hosted-guide/upgrade-server) and the production checklist (https://docs.temporal.io/self-hosted-guide/production-checklist).
- **SDK source:**
  - temporalio 1.30.0: `temporalio/worker/_tuning.py` (`CustomSlotSupplier`) and `temporalio/common.py` (`Priority`).
- **Linux:**
  - NAPI: https://docs.kernel.org/networking/napi.html
  - EEVDF: https://docs.kernel.org/scheduler/sched-eevdf.html
  - cgroup v2: https://docs.kernel.org/admin-guide/cgroup-v2.html
  - RCU: https://docs.kernel.org/RCU/whatisRCU.html
  - epoll(7): https://man7.org/linux/man-pages/man7/epoll.7.html
  - systemd.service and systemd.timer (`Restart=`, `StartLimitBurst=`, `Persistent=`).
- **Agent OS and actors:**
  - AIOS (arXiv 2403.16971);
  - Letta;
  - Erlang/OTP design principles, supervisor behaviour;
  - Microsoft Orleans (grains, reminders);
  - Akka (mailboxes, passivation).
- **Peer platforms:** Inngest (debounce, batch, throttle), Restate (virtual objects), Hatchet (concurrency strategies), LangGraph (double-texting), DBOS (queues).
- **Drivers:** whatsmeow, https://pkg.go.dev/go.mau.fi/whatsmeow (`Client.SynchronousAck`).
- **PostgreSQL:** "Postgres LISTEN/NOTIFY does not scale" (Recall.ai), https://recall.ai/blog/postgres-listen-notify-does-not-scale
- **Related RFCs:** RFC-0002 (agent context and memory), RFC-0004 (Temporal SDK adoption).
