# OpenCompany Scripts Reference

## Quick Start

```bash
npm install -g @zeenie-ai/opencompany
company start
```

Open the app URL — `http://localhost:${PYTHON_BACKEND_PORT}` (the port
is declared once in `.env.template`; see [SETUP.md](./SETUP.md) for the
default).

## CLI Commands (`company`, Python Typer app under `cli/`)

The CLI is the single orchestration surface — every `bun run <verb>` at
the root is a thin wrapper over `python -m cli <verb>`. The old
`scripts/{start,stop,build,clean,docker}.js` orchestrators were retired
(each `cli/commands/<verb>.py` docstring records what it replaced).
`machina` remains as a deprecated alias of `company` (prints a
deprecation warning; kept for upgrade compatibility).

| Command | Description |
|---------|-------------|
| `company start` | Production mode, single port: uvicorn serves API + WS + built SPA on `PYTHON_BACKEND_PORT`. Optional daemons (Temporal dev server, WhatsApp) are backend-owned, started from the lifespan when enabled |
| `company dev` | Start in dev mode (Vite HMR + uvicorn). `--force` re-bundles Vite deps (recovers "Outdated Optimize Dep"); `--daemon` binds backend to 0.0.0.0 |
| `company serve` | Single-port production runtime (uvicorn serves API + WS + built SPA; optional daemons incl. the Node.js executor are backend-spawned on demand) — the systemd `ExecStart` on deployed VMs |
| `company stop` | Stop all services and free configured ports |
| `company build` | Full production build (bun install → client → sidecar → uv sync → bytecode → temporal binary). Step [0/6] scaffolds `.env` from `.env.template` when missing, generating fresh random secrets (`secrets.token_hex(24)`) for `SECRET_KEY` / `JWT_SECRET_KEY` / `API_KEY_ENCRYPTION_KEY` instead of the dev placeholders; an existing `.env` is untouched |
| `company clean` | Stop services, then remove build artifacts, node_modules, `.venv`, repo-local state (preserves `.opencompany/{workflows,deploy,packages}`) |
| `company deploy up/status/destroy` | Self-deploy a login-gated VM (gcloud preflight + Terraform; see `cli/commands/deploy/`) |
| `company daemon start/stop/status/restart` | Detached backend management (PID file under user data dir) |
| `company version sync` | Propagate the root package.json version |
| `company docs nodes [--check]` | Regenerate (or verify) the `docs-internal/node-logic-flows/` index |

There is no `help` verb: `company` with no arguments, `company --help`, and
`company <verb> --help` print Typer's help (`no_args_is_help=True` in `cli/cli.py`).

### Dependency checks

`start` and `build` verify Node.js 22+, Python 3.12+, and uv before
running.

---

## package.json scripts (run with bun)

Run with `bun run <script>` from the project root (`package.json` is
the source of truth). The dev package manager is bun@1.4.0 —
`scripts/preinstall.js` rejects `npm install` in a source checkout.
The commands below are quoted verbatim from `package.json`; two of them
(`client:start`, `test:frontend`) still spell `npm run` internally
because they invoke another package's script, which bun executes on
Node either way (recorded follow-up).

### CLI wrappers

| Script | Command |
|--------|---------|
| `start` / `dev` / `serve` / `build` / `clean` / `stop` / `deploy` | `python -m cli <verb>` |
| `start:temporal` | `cross-env TEMPORAL_ENABLED=true python -m cli start` |
| `daemon:start` / `daemon:stop` / `daemon:status` / `daemon:restart` | `python -m cli daemon <verb>` |
| `version:sync` | `python -m cli version sync` |
| `docs:nodes` / `docs:nodes:check` | `python -m cli docs nodes [--check]` |

### Service scripts

| Script | Command | Description |
|--------|---------|-------------|
| `client:start` | `cd client && npm run start` | React frontend (Vite dev server) |
| `python:start` | `cd server && uv run python main.py` | Backend only (`main.py` reads `HOST` / `PYTHON_BACKEND_PORT` from the env) |
| `python:daemon` | `cd server && cross-env HOST=0.0.0.0 uv run python main.py` | Backend only, LAN-reachable |
| `temporal:worker` | `cd server && uv run python -m services.temporal.worker` | Standalone Temporal worker |

The Temporal dev server is backend-owned: the FastAPI lifespan starts it via `TemporalServerRuntime.ensure_started()` when `TEMPORAL_ENABLED` (see [Temporal Architecture](./TEMPORAL_ARCHITECTURE.md)). The official `temporal` CLI is downloaded by `pooch` to `<DATA_DIR>/packages/temporal/` (= `~/.opencompany/packages/temporal/` by default) during `company build`.

### Tests

| Script | Command |
|--------|---------|
| `test` | backend + frontend suites |
| `test:backend` | `cd server && uv run pytest tests/ -v` |
| `test:frontend` | `cd client && npm run test` (vitest) |
| `test:nodes` | node-plugin tests with handler coverage |

### Lifecycle hooks

| Script | File | Purpose |
|--------|------|---------|
| `preinstall` / `preuninstall` | `scripts/preinstall.js` | Removes the legacy `machinaos` global package / stale temp dirs before (un)install |
| `postinstall` | `scripts/postinstall.js` | End-user install pipeline for the npm tarball (delegates to `scripts/install.js`) |

---

## Files actually in `scripts/`

| File | Purpose |
|------|---------|
| `install.js` | npm-tarball install pipeline (npm/uv install for end users — dev workspaces use bun; client build only when `client/dist` is missing from the tarball, `uv sync`, bytecode compile, CLI runtime venv, non-fatal Temporal binary fetch; the Node.js sidecar `dist/index.js` ships pre-built in the tarball) — mirrors `company build`; the compileall command shape is locked in sync by `cli/tests/test_release_pipeline_config.py` |
| `preinstall.js` | Legacy-package/temp cleanup (also runs on uninstall) |
| `postinstall.js` | npm lifecycle entry that guards recursion and invokes install.js |
| `migrate_icons.py`, `migrate_skill_icons.py` | One-off icon-migration utilities (historical) |

(`serve-client.js` was retired July 2026: `company start` is single-port —
the backend serves the built SPA itself via `SERVE_STATIC_CLIENT`.)

There is no Docker tooling: Docker Compose support was removed
(historical topology preserved in
[deployment_legacy.md](./deployment_legacy.md)); deployment is
`company deploy` (Terraform → GCP VM → systemd).

---

## Environment Variables

Key variables in `.env` (see `.env.template` for the full list):

### Ports
| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_CLIENT_PORT` | `.env.template` | App port (Vite dev server; proxies backend prefixes). Equal to `PYTHON_BACKEND_PORT` in production |
| `PYTHON_BACKEND_PORT` | `.env.template` | Backend port (`.env.dev` moves it one up in dev, behind the Vite proxy) |
| `WHATSAPP_RPC_PORT` | `.env.template` | WhatsApp API port (plugin-owned) |
| `NODEJS_EXECUTOR_PORT` | `.env.template` | Node.js code-executor sidecar (plugin-owned) |
| `TEMPORAL_FRONTEND_GRPC_PORT` / `TEMPORAL_UI_PORT` | `.env.template` | Temporal gRPC / Temporal Web UI |

All values live in the serial block declared at the top of `.env.template`; no
code or doc should carry the numerals.

### Features
| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPORAL_ENABLED` | (see `.env.template`) | Temporal execution engine |
| `REDIS_ENABLED` | false | Redis cache (SQLite fallback when false) |

---

## Required Dependencies

| Dependency | Version | Install |
|------------|---------|---------|
| Node.js | 22+ | https://nodejs.org/ |
| Python | 3.12+ (CLI); server venv accepts 3.11–3.12 | https://python.org/ |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| bun | 1.4.x | official installer (https://bun.sh); root `packageManager` pin read by `oven-sh/setup-bun` in CI |

---

## Quick Reference

```bash
# Development
company dev            # app at PYTHON_BACKEND_PORT (Vite HMR; backend one port up via .env.dev, behind the proxy)
company dev --force    # ...forcing a Vite dependency re-bundle
company start          # Production mode (single port: PYTHON_BACKEND_PORT)
company stop           # Stop all services

# Build / clean
company build          # Full production build
company clean          # Clean everything (keeps workflows/deploy/packages state)

# Deploy
company deploy up --provider gcp --owner-email you@example.com
company deploy status
company deploy destroy
```
