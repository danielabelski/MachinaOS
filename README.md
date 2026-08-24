<img width="1584" height="672" alt="OpenCompany banner" src="https://github.com/user-attachments/assets/cebd0198-4c09-4757-9407-a7ad79a7d71e" />

# OpenCompany

<a href="https://www.npmjs.com/package/@zeenie-ai/opencompany" target="_blank"><img src="https://img.shields.io/npm/v/%40zeenie-ai%2Fopencompany.svg" alt="npm version"></a>
<a href="https://opensource.org/licenses/MIT" target="_blank"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
<a href="https://discord.gg/c9pCJ7d8Ce" target="_blank"><img src="https://img.shields.io/discord/1455977012308086895?logo=discord&logoColor=white&label=Discord" alt="Discord"></a>
<a href="https://deepwiki.com/zeenie-ai/OpenCompany" target="_blank"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>

**Your own AI workforce, running on your own machine.**

OpenCompany is an open-source, self-hosted canvas for AI agent workflows — think n8n, built agent-first. Drag, drop, and connect AI agents to your email, calendar, messages, browser, phone, and 30 other services, with 146 nodes across 31 categories to build from. No code required. No subscription. No usage limits. Bring your own API keys, or run models locally with Ollama / LM Studio for free.

**[Read the docs →](https://docs.opencompany.sh)**

## Quick Start

**Prerequisites:** Node.js 22+, Python 3.12

```bash
npm install -g @zeenie-ai/opencompany
company start
```

Open http://localhost:5678 and click the key icon (**API Credentials**) in the toolbar to connect your first AI provider.

<details>
<summary><b>Run from source (for contributors)</b></summary>

```bash
# install bun (https://bun.sh) — Windows: powershell -c "irm bun.sh/install.ps1 | iex"
curl -fsSL https://bun.sh/install | bash
git clone https://github.com/zeenie-ai/OpenCompany.git OpenCompany
cd OpenCompany
bun install
bun run build
bun run dev
```

The `dev` task starts the Vite client (with HMR) at http://localhost:5678 — the same URL as production — proxying API/WebSocket traffic to the Python backend on :5679; optional daemons (WhatsApp, Temporal) are spawned by the backend on demand. Every port is declared in `.env.template` and overridable in `.env`; nothing is hardcoded. See [SETUP.md](docs-internal/SETUP.md) and [SCRIPTS.md](docs-internal/SCRIPTS.md) for details, and [CONTRIBUTING.md](CONTRIBUTING.md) for the codebase map and contribution recipes.

**Upgrading from MachinaOS?** Existing `~/.machina` and checkout-local `.machina` state is detected when the new `.opencompany` location does not yet exist, so databases and deployment state are not stranded. The `machina` command remains available as a deprecated legacy alias; new scripts should use `company`.

</details>

## See it in action

**Hello-world setup, end to end ↓**

https://github.com/user-attachments/assets/a5a5583f-bb5f-4d27-a387-8522c556e89e

**AI building itself for complex tasks ↓**

https://github.com/user-attachments/assets/035a2293-0837-4969-8b9d-8d680e023b89

**Multiple specialized loop agents orchestrating ↓**

https://github.com/user-attachments/assets/3d25e9a3-f7b9-4760-8b9a-6de1e5a19cad

## How It Works

[![How OpenCompany Works](docs/diagrams/how-it-works.svg)](https://raw.githubusercontent.com/zeenie-ai/OpenCompany/main/docs/diagrams/how-it-works.svg)

Pick nodes from the palette, drag them onto a canvas, connect them with lines, and give your AI agent some memory and skills. Press **Run** on a node to test it in place, or press **Start** to deploy the whole workflow as a durable background listener — waiting for emails, responding to messages, checking in on a schedule, doing the work you'd rather not.

[![Default workflows that ship with OpenCompany](docs/diagrams/default-workflows.svg)](https://raw.githubusercontent.com/zeenie-ai/OpenCompany/main/docs/diagrams/default-workflows.svg)

Three example workflows load automatically on first launch. Open them on the canvas to see exactly how the pieces fit together, then edit any node and save your own version.

## What You Can Build

- **Personal AI assistants that remember.** A chat assistant that knows your calendar, reads your inbox, and follows up on tasks. Connect a Context node and the conversation durably persists across every trigger firing — inspect the agent's live context in real time from the canvas (formatted transcript or raw JSON). A durable Memory tool holds the facts, preferences, and decisions the agent explicitly remembers, with vector search for long-term recall.
- **Durable agent teams.** Hire an **AI Employee** or Orchestrator as a team lead, connect specialist agents through `input-teammates`, and the lead assigns bounded work through its built-in Task Manager. Tasks are durably queued, run up to three descendants in parallel, require lead acceptance, and remain visible in the read-only Team Monitor.
- **Automations that run themselves.** Recurring jobs ("every weekday at 9 AM, summarize my unread emails"), event-driven replies ("when a customer texts on WhatsApp, draft a response"), and multi-step background pipelines. Any workflow can also expose a live `/webhook/{path}` HTTP endpoint that fires on GET, POST, PUT, DELETE, or PATCH.
- **Email, calendar, and document workflows.** Send and search Gmail, manage Calendar, Drive, Sheets, Tasks, and Contacts; **Microsoft 365** mail and calendar over the Graph API. Read any inbox over IMAP (Gmail, Outlook, Yahoo, iCloud, ProtonMail, Fastmail, or custom servers) — including a polling trigger that fires a workflow on every new message.
- **Messaging bots.** Send and receive on **WhatsApp** (personal — groups, contacts, newsletter channels), **WhatsApp Business** (official Meta Cloud API: templates, media, interactive messages, signed webhooks), **Telegram** (bots with owner detection), **Discord** (bot with gateway message triggers, slash commands, and OAuth2), and **Twitter/X** (post, reply, search). A unified social node normalizes incoming messages into one format so the same workflow handles them all.
- **Voice and language.** Provider-abstracted text-to-speech and speech-to-text (OpenAI, ElevenLabs, Deepgram, Groq, Sarvam) with reference-based audio that flows between nodes, plus translate / transliterate / detect-language nodes (DeepL, Sarvam, or any connected LLM).
- **Phone control from a workflow.** Pair your Android phone via QR code and control it from any agent: battery and network status, app launching, WiFi / Bluetooth / airplane toggles, camera, sensors, media playback — 16 device services.
- **Web automation and research.** An interactive browser with accessibility-tree navigation (click, type, screenshot); an alpha harness that drives your *real* Chrome over CDP; scraping with Crawlee and Apify actors (Instagram, TikTok, LinkedIn, Facebook, YouTube, Google Search); search via DuckDuckGo (free), Brave, Serper, and Perplexity; residential proxies with geo-targeting and rotation.
- **Code, deploys, and pull requests.** Run Python / JavaScript / TypeScript in per-workflow sandboxed workspaces, keep dev servers alive with the Process Manager node (output streams to the Terminal tab), open and merge PRs with the **GitHub** node, ship with the **Vercel** node, manage DNS and analytics with **Cloudflare**, and drive Compute Engine / Cloud Run / Storage with the **Google Cloud** node — all four authenticate through their own CLIs, no token pasting required. A **Gallery** node gives every workflow a visual file explorer with previews and drag-to-parameter assignment.
- **Local data and vision.** Give agents typed, bounded access to workspace files and operator-approved external folders through the dataSource tool, and vision for every host model via the visionAnalyze delegate — images travel as native content blocks, never pasted base64.
- **Payments.** **Stripe** action node (charges, subscriptions) plus a signed-webhook receiver for reacting to payment events in real time.
- **Your own knowledge base.** RAG out of the box: parse PDFs and HTML, chunk, embed locally or via OpenAI, store in ChromaDB / Qdrant / Pinecone, query from any agent.

## AI Capabilities

### 13 providers, 12 dedicated model nodes — bring your own keys or run locally

| Provider     | Notes                                                                    |
|--------------|--------------------------------------------------------------------------|
| OpenAI       | GPT-5.6 Sol / Terra / Luna (+ Pro variants), GPT-5.5, GPT-4.1            |
| Anthropic    | Claude Opus 5, Fable 5, Sonnet 5, Opus 4.8 / 4.7 — with extended thinking |
| Google       | Gemini 3.6 / 3.5 Flash, 3.1 Pro — with reasoning budgets                 |
| xAI          | Grok 4.20, 4.20 multi-agent, 4.3 — selectable from any agent             |
| DeepSeek     | DeepSeek V4 Flash / Pro                                                  |
| Kimi         | Kimi K3                                                                  |
| Mistral      | Mistral Large / Medium / Small, Codestral                                |
| Groq         | GPT-OSS-120b and more (ultra-fast inference)                             |
| Cerebras     | GPT-OSS-120b (custom AI hardware)                                        |
| Sarvam       | Indic-first models (sarvam-105b, 128K context)                           |
| OpenRouter   | 200+ models via one unified API                                          |
| **Ollama**   | Run any local model on your machine — free, private, offline             |
| **LM Studio**| Run any local model with a desktop app — free, private, offline          |

Every provider talks to its vendor SDK directly through a native layer — no translation wrapper in between. xAI is the one provider without a standalone chat-model node; it is chosen from the agent's own provider dropdown, which is why there are 13 providers but 12 nodes.

Local providers (Ollama, LM Studio) are first-class — context length is detected automatically from your running server (LM Studio additionally reports vision and tool-use capability). No paid API needed.

### 20 agent node types

| Agent              | Specialized for                                                          |
|--------------------|--------------------------------------------------------------------------|
| **AI Agent** / **Chat Agent** | The general-purpose agents most workflows start from          |
| **AI Employee** / **Orchestrator** | Team leads that coordinate other agents                  |
| Android Agent      | Phone control                                                            |
| Web Agent          | Browser automation, scraping, search                                     |
| Coding Agent       | Writing and running code (Python / JS / TS)                              |
| Productivity Agent | Gmail, Calendar, Drive, Sheets, Tasks, Contacts                          |
| Social Agent       | WhatsApp, Telegram, Twitter messaging                                    |
| Task Agent         | Scheduling, reminders, cron jobs                                         |
| Travel Agent       | Maps, location lookup, planning                                          |
| Payments Agent     | Stripe + financial workflows                                             |
| Consumer Agent     | Customer support, order management                                       |
| Claude Code Agent  | Anthropic's Claude Code CLI for advanced coding sessions                 |
| Codex Agent        | OpenAI Codex CLI integration                                             |
| RLM Agent          | Recursive Language Model — write code that calls itself recursively      |
| Autonomous Agent   | Code-mode loops that reduce token usage 80-98%                           |
| Tool Agent         | General-purpose tool orchestration                                       |
| Vertex Agents      | Google Vertex managed agents, plus an admin node for their lifecycle     |

The Claude Code agent keeps warm interactive sessions in a pool (same session across turns, automatic resume after a crash) and runs on interactive billing — a Claude subscription login works instead of per-token API cost. The Codex agent sandboxes parallel tasks in git worktrees.

### Skills you can edit yourself

Skills are short markdown files that teach an agent how to do something well — when to use which tool, what arguments to pass, common mistakes to avoid. Edit them in the UI; changes apply immediately. 77 ship built in across 19 folders, covering Android control, Google Workspace, social messaging, web research, local data and vision, coding, terminal use (Bash, PowerShell, WSL, Nushell), payments, deployment, and more — and you can drop your own into `.opencompany/skills/`, where they override the built-ins of the same name.

### Conversations that survive, memory that scales

Connect a **Context** node and an agent's conversation is durably stored per workflow generation — every trigger firing (a chat message, a completed delegated task) continues the same conversation, and the panel shows the live transcript in real time. When token usage approaches the model's context limit (80% by default), the agent compacts: the shared native LLM layer asks the selected model for a five-section summary — Task Overview, Current State, Important Discoveries, Next Steps, Context to Preserve — and continues from it, with the system prompt untouched and the summary carried across firings. Provider-reported usage is aggregated across the loop; session token and cost metrics are persisted on the memory-connected in-process path, while durable Temporal runs return their aggregate usage in the execution result.

### Cost tracking, built in

Memory-connected agent runs calculate USD cost from provider-reported usage when that usage is available. See tracked spend in the API Credentials panel, and configure pricing in `pricing.json` for custom model pricing. This is not a universal audit log of every LLM or third-party API request.

## Built Like Production Infrastructure

- **Durable execution via Temporal.** Ordinary node and agent-support activities retry transient failures with bounded backoff; billed `AgentWorkflow` LLM-step activities run once to avoid automatic double billing after ambiguous failures. Cron schedules have a 24-hour catch-up window so missed ticks backfill, and per-queue worker pools scale horizontally. Falls back to a local executor when disabled.
- **Credentials encrypted at rest.** API keys and OAuth tokens live in a separate `credentials.db`, encrypted with Fernet (AES-128-CBC + HMAC-SHA256) and a PBKDF2-SHA256 key at 600,000 iterations. Nothing leaves your machine.
- **Login-gated by choice.** Runs open on localhost by default; flip on single-owner JWT auth (or multi-user mode) for shared and cloud deployments — `company deploy` enables it automatically.

## The Canvas

- **12 visual themes** — light, dark, Renaissance, Greek, Edo, Steampunk, Atomic, Cyber, Wasteland, Rot, Plague, Surveillance — each with its own icon set, sound pack, and decorative ornaments. Animations honor `prefers-reduced-motion`.
- **Drag-to-map outputs** from one node's output directly onto another's input fields.
- **Live execution animations** — nodes glow while running, AI agents show iteration counts, errors surface inline.
- **Chat + Console panel** — a resizable bottom panel with a chat pane for talking to trigger nodes, plus Console and Terminal tabs for logs and live process output.
- **Component palette** with search, categories, and a Normal/Dev mode toggle that hides advanced nodes when you don't need them.
- **4-step onboarding wizard** for first-time users, replayable any time from Settings.

## For Developers

Want to add a node, LLM provider, skill, or integration? One Python file = one node. The backend owns all the schemas; the frontend renders from them automatically. No frontend code required for most extensions.

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — codebase map, architecture diagrams, contribution recipes
- **[server/nodes/README.md](server/nodes/README.md)** — 5-minute plugin recipe + folder map
- **[docs-internal/](docs-internal/)** — deep-dive architecture docs (execution engine, Temporal, LLM layer, credentials, event system, performance, build pipeline)
- **[CLAUDE.md](CLAUDE.md)** — comprehensive project memory (great for AI-assisted contributions)
- **Hosted docs:** https://docs.opencompany.sh/
- **DeepWiki:** https://deepwiki.com/zeenie-ai/OpenCompany

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the fork/branch/PR workflow, the repository map, and recipes for adding a node, LLM provider, or skill.

One note on scope: connector and provider lists are kept deliberately narrow. The Apify node runs any actor through its `custom` option, and agents reach any OpenAI-compatible endpoint through the existing provider path — so a new first-class preset needs a reason beyond "my service could be in the dropdown too."

## Community

[Discord](https://discord.gg/c9pCJ7d8Ce) — the fastest way to get help, request features, and follow design discussions.

## License

[MIT](LICENSE) — © 2025 MachinaOs, © 2026 OpenCompany contributors.
