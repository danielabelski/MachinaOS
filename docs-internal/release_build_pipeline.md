# Release Build Pipeline

Compile-step plan for the npm distribution `@zeenie-ai/opencompany`. Goal: cut cold-start ~20s further on top of the lazy-LangChain fix already in v0.0.76, shrink the Vite main bundle below 200 KB gz, and drop the `tsx` interpreter cost from the Node.js sidecar.

User scope (confirmed before this work): stay on npm distribution; **no** Nuitka/PyOxidizer standalone binary channel; optimize Vite output too; **skip** the plugin-walker (~11s) and service-status-refresh (~20s) hotspots — those are runtime concerns, not compile-time.

## Tooling

| Layer | Tool | Why |
|---|---|---|
| TypeScript type-check | `typescript@7` (the native Go compiler) at the **repo root** | **5.9× faster** on `--noEmit` — measured 2026-07-26 on this codebase, 3 warm runs each: **~820 ms** vs **~4830 ms** under `tsc` 5.9.3. Type-check only; Vite/esbuild keep producing the actual JS bundles. Lives in root `devDependencies`, exact-pinned, never ships to users. |
| Vite output | `manualChunks` + `target: 'es2022'` | Split heavy libs (reactflow, radix-ui, lobehub, react-markdown stack) so the main bundle no longer hits the 1500KB warning ceiling. ES2022 unlocks `findLast` / optional-chaining-assignment without polyfills (Chrome 94+, FF 93+, Safari 15.4+ — within React 19 / Tailwind 4 baseline). |
| Node sidecar | `esbuild` bundle to `dist/index.js`, run via `node` | Drops tsx interpreter startup (~500ms-1s every server boot). `--packages=external` keeps Express in `node_modules/` for patch flow. |
| Python | `python -m compileall -q -j 0 <project dirs>` + `[tool.uv] compile-bytecode = true` | Pre-compile bytecode. Implemented as step `[5/6]` in [`cli/commands/build.py`](../cli/commands/build.py) (`COMPILEALL_SOURCE_DIRS` constant lists the dirs); `server/pyproject.toml`'s `compile-bytecode = true` makes `uv sync` (step `[4/6]`) compile `.venv/` site-packages too. No `-O`: every runtime launches python without `-O`, and per PEP 488 a non-optimized interpreter only loads plain `.pyc` — the earlier `-O` invocation produced `.opt-1.pyc` that nothing ever loaded (fixed 2026-07-14; ~30-50s cold-start gain, see [performance.md](performance.md)). |

## Package manager (bun)

The dev workspace runs on **bun** (migrated from pnpm@9.15.0, 2026-08; pinned
via `"packageManager": "bun@1.4.0"` — CI's `oven-sh/setup-bun` reads that pin).
Facts that are load-bearing, each verified during the migration:

- **Scope**: bun installs the workspace and runs scripts; **Node 22 stays the
  runtime** (vite/vitest/eslint/the sidecar run on node via shebang — never
  pass `--bun`). Everything npm-facing is deliberately untouched: end-user
  `npm install -g @zeenie-ai/opencompany`, the backend's
  `npm install --prefix <DATA_DIR>/packages/` shared tree, the sidecar's
  user-package endpoints, and `npm publish --provenance` in release.yml.
- **Gate**: `scripts/preinstall.js` rejects non-bun installs in a source
  checkout. It keys on `bunfig.toml` existing (committed, excluded from the
  tarball by the `files` allowlist, so end-user npm installs never trigger it)
  and on `npm_config_user_agent` **starting with** `bun` — bun's UA is
  `bun/x.y.z npm/? node/...`, so a substring match on "npm" would misfire.
- **Layout**: `bunfig.toml` pins `linker = "isolated"` — pnpm-style symlinked
  `node_modules` with the store at `node_modules/.bun/` (NOT `.pnpm/`). The
  phantom-dependency guard, the `typeRoots` tsconfig workaround, and the
  `vite-env.d.ts` triple-slash reference all depend on this layout.
- **Lockfile**: the tracked text `bun.lock` is **lockfileVersion 3** — forced
  by the version-ranged keys in the top-level `overrides` block (the security
  pins formerly under `pnpm.overrides`, moved verbatim). Older bun cannot read
  it; the `packageManager` pin is the effective read-floor.
- **Sharp edges** (each bites silently): declaring `trustedDependencies`
  REPLACES bun's default trusted list rather than extending it — do not declare
  it (esbuild's install script would stop running); never pass
  `--omit=optional` (it drops the per-platform rollup/esbuild/TS7 binaries,
  oven-sh/bun#16696); bun 1.4 rejects the space-separated `--cwd ..` form
  (`bun --cwd=.. run <script>` is required — this is why the client typecheck
  script uses `=`); and `--filter` must come AFTER the `run` subcommand.
- **Lost guard**: pnpm's `strict-peer-dependencies` has no bun equivalent (bun
  never errors on peer conflicts, oven-sh/bun#9135). The
  `test_client_keeps_typescript_5_for_typescript_eslint` CLI test is now the
  only check keeping client `typescript` inside typescript-eslint's peer range.
- Config invariants (gate source, manifest shape, bunfig linker, workspace
  paths) are locked by `cli/tests/test_release_pipeline_config.py`; the
  workspace-member oracle in `cli/tests/conftest.py` parses the root
  `workspaces` array directly (bun has no `pnpm list --json` equivalent).

## Implementation steps

### 1. TypeScript 7 type-check

The native Go compiler shipped inside mainline `typescript` at the 7.0 GA
(2026-07-08). The old `@typescript/native-preview` channel — whose binary was
named `tsgo` — stopped publishing the day before and must not come back.

**The gate lives at the repo root, and that placement is forced, not stylistic:**

- `client` already depends on `typescript@^5.9.3`, which typescript-eslint needs
  (it loads the TS API at module scope and its peer range excludes 6.x/7.x). A
  single manifest cannot declare `typescript` twice.
- `typescript@7` and `typescript@5` **both** ship a `tsc` bin. Co-locating them
  would leave bun's bin-link conflict resolution (under the isolated linker)
  deciding which compiler gates
  CI. (No collision exists today only because `native-preview`'s bin was `tsgo`.)
- Root declares no `typescript` and no `typescript-eslint`, so there is no peer
  conflict and nothing else claims `tsc`.

- root `package.json` → `devDependencies: { "typescript": "7.0.2" }`, **exact —
  no caret**, so the compiler that gates CI never moves on an unrelated install;
  plus `"typecheck": "tsc --noEmit -p client/tsconfig.json"`
- `client/package.json` → `"typecheck": "bun --cwd=.. run typecheck"` (delegates up,
  so the CI command and its guard test stay byte-identical)
  - `"typecheck:tsc": "tsc --noEmit"` — resolves client's own 5.9.3. A **second
    opinion for triaging a red gate**, NOT an equivalent check and NOT a revert
    path: the two compilers check different programs under different default
    rules. Nothing in CI runs it, so verify it independently before trusting it.
- `.github/workflows/predeploy.yml` → `bun run --filter react-flow-client typecheck` in the
  `build-and-lint` job. (This gate lives in `predeploy.yml`, not `release.yml`.) The cross-OS
  `test-build-start` matrix additionally runs `bun run tsc --version`, because the TS7
  compiler is a per-platform Go binary delivered via `optionalDependencies` — the type-check
  itself runs only on ubuntu, so without that step the darwin-arm64 and win32-x64 binaries
  would be installed on every matrix run and never executed.

### 2. Vite manualChunks + target

- `client/vite.config.js` → extend `build` block:
  - `target: 'es2022'`
  - `chunkSizeWarningLimit: 600` (down from 1500)
  - `rollupOptions.output.manualChunks` mapping:
    - `vendor-react`: `react`, `react-dom`, `react-hook-form`, `@hookform/resolvers`
    - `vendor-flow`: `reactflow`
    - `vendor-radix`: `@radix-ui/*`, `radix-ui`
    - `vendor-icons`: `lucide-react`, `@lobehub/icons`
    - `vendor-query`: `@tanstack/react-query`, `@tanstack/query-sync-storage-persister`, `@tanstack/react-query-persist-client`, `@lukemorales/query-key-factory`
    - `vendor-markdown`: `react-markdown`, `remark-gfm`, `remark-breaks`, `prismjs`, `react-simple-code-editor`, `@uiw/react-json-view`
    - `vendor-misc`: `idb-keyval`, `fuzzysort`, `cmdk`, `sonner`, `qrcode.react`

Keep `sourcemap: analyze` (already correct), keep React Compiler config.

### 3. Node sidecar esbuild bundle

- `server/nodejs/package.json` → add `esbuild` devDep; replace scripts:
  - `"build": "esbuild src/index.ts --bundle --platform=node --target=node22 --format=esm --packages=external --outfile=dist/index.js"`
  - `"start": "node dist/index.js"` (was `tsx src/index.ts`)
  - keep `"dev": "tsx watch src/index.ts"`
- `server/nodejs/.gitignore` → new file: `dist/`

### 4. Python bytecode pre-compile

Two halves (both required; see [performance.md](performance.md) for the
2026-07-14 cold-boot measurements that motivated the split):

- `server/pyproject.toml` → `[tool.uv] compile-bytecode = true` makes
  `uv sync` (step `[4/6]`) compile all `.venv/` site-packages at install
  time. uv's default is **false** — without this, every dependency `.py`
  compiles lazily on the first import after a fresh sync.
- [`cli/commands/build.py`](../cli/commands/build.py) → step `[5/6]`
  covers the project's own source:
  ```python
  run(
      uv_run("python", "-m", "compileall", "-q", "-j", "0", *COMPILEALL_SOURCE_DIRS),
      cwd=server_cwd,
      check=False,  # missing pyc is non-fatal — runtime regenerates as needed
  )
  ```
  No `-O`: runtimes never launch python with `-O`, so plain `.pyc` is the
  only bytecode flavor that gets loaded (PEP 488). The list of source
  dirs is the public `cli.commands.build.COMPILEALL_SOURCE_DIRS`
  constant — `scripts/install.js` mirrors it.

The npm tarball still excludes `__pycache__/` per `package.json` `files` (cross-Python-minor pyc fragility) — `compileall` runs on the user's machine via `company build` or `scripts/install.js` post-install.

### 4a-bis. `.env` scaffolding with fresh secrets (step `[0/6]`)

- When no `.env` exists, `company build` step `[0/6]` scaffolds it from `.env.template` and replaces the dev placeholder values of `SECRET_KEY` / `JWT_SECRET_KEY` / `API_KEY_ENCRYPTION_KEY` with fresh `secrets.token_hex(24)` values (`_scaffold_env_secrets` in `cli/commands/build.py`). An existing `.env` is never modified. The placeholder literals themselves are the SSOT frozenset `DEV_SECRET_LITERALS` in `server/core/config.py`; server startup logs a non-fatal error banner via `dev_secret_offenders()` when placeholders are detected with auth enabled or `DEPLOYMENT_MODE != local`.

### 4b. Temporal binary fetch + DATA_DIR parity

- Step `[6/6]` runs `uv run python -m services.temporal._install`, which pooch-downloads the official `temporal` CLI into `<DATA_DIR>/packages/temporal/` (= `~/.opencompany/packages/temporal/` by default). Pre-fetching at build time turns the ~114 MB download into a sub-second cache hit on first `company start`. The download uses an explicit `HTTPDownloader(timeout=300)` (per-socket-read timeout — slow links can finish; pooch's 30 s default aborted them). Fatality differs by entry point: `company build` keeps the step **fatal** (locked by `test_temporal_install_is_fatal_on_failure`), while npm postinstall (`scripts/install.js`) wraps it in a **non-fatal** try/catch because `TemporalServerRuntime._pre_spawn()` re-downloads lazily on first `company start`. The cache survives `company clean` (`packages` ∈ `_OPENCOMPANY_KEEP`), so clean+build cycles don't re-download.
- **`company build` layers `.env.dev` first.** `build_command()` calls `cli.config.load_dev_overrides(root)` before the install steps, so the build's `DATA_DIR` matches what the runtime sees. Without it, a repo checkout's `company build` read `DATA_DIR=~/.opencompany` from `.env.template` and installed Temporal under user home, but `company dev` then read `DATA_DIR=.opencompany` from `.env.dev` and re-downloaded into `<repo>/.opencompany/` — a redundant ~114 MB fetch on every fresh clone.
- **Safe for global installs.** `.env.dev` is git-committed for contributors but is NOT in the npm `files` list, so an npm-distributed copy has no `.env.dev` — `load_dev_overrides` is a no-op and everything falls through to the `.env.template` default (`DATA_DIR=~/.opencompany`), matching `company start` / `company daemon`.

### 5. Wire bundle + compileall into install.js

- `scripts/install.js` → after `uv sync`:
  1. `npm --prefix server/nodejs run build` — produce `dist/index.js`
  2. `python -m compileall -q -j 0 <COMPILEALL_SOURCE_DIRS>` — same shape as build.py (no `-O`; locked in sync by `cli/tests/test_release_pipeline_config.py`)

Idempotent on re-runs (compileall only rewrites stale pyc; esbuild is deterministic).

### 6. Tarball verification

- `npm pack --dry-run` after the change. Confirm `server/nodejs/dist/index.js` is included (existing `server/` glob already covers it). Confirm no `__pycache__/` leakage.

## Critical files

| File | Action |
|---|---|
| root `package.json` | + `typescript@7` devDep (exact-pinned), + root `typecheck` script |
| `client/package.json` | `typecheck` delegates to the root gate; keeps `typescript@^5.x` for typescript-eslint |
| `client/vite.config.js` | + manualChunks, target, lower warning |
| `server/nodejs/package.json` | + esbuild devDep, build script, change start |
| `server/nodejs/.gitignore` | new — ignore `dist/` |
| `cli/commands/build.py` | + compileall step (`[5/6]`, plain `.pyc` — no `-O`), `COMPILEALL_SOURCE_DIRS` constant |
| `scripts/install.js` | + sidecar bundle + compileall calls |
| `server/pyproject.toml` | `[tool.uv] compile-bytecode = true` — `uv sync` compiles `.venv/` site-packages |
| `.github/workflows/predeploy.yml` | + typecheck gate in `build-and-lint`; + `tsc --version` in the cross-OS matrix |

## Verification

1. `bun run --filter react-flow-client typecheck` → <5s, zero errors.
2. `ANALYZE=1 bun run --filter react-flow-client build` → open `client/dist/stats.html`. Expect: no chunk above 600 KB gz, main < 200 KB gz, `vendor-flow` split.
3. `cd server/nodejs && npm run build && node dist/index.js` → starts on :5682 in <100ms.
4. `cd server && uv run python -m compileall -q -j 0 services` → plain `__pycache__/*.pyc` present (no `.opt-1.pyc` — nothing loads those).
5. Cold-start: clean install + `company start > start.log 2>&1` → `Application startup complete` at ≤+50s (was +66.9s).
6. `npm pack --dry-run` → `server/nodejs/dist/index.js` included; no `__pycache__/`; tarball size ≤ v0.0.76.
7. Smoke: `company start` → load http://localhost:5678 → run "AI Assistant" example → agent responds.

## Out of scope (future work)

- Nuitka / PyOxidizer standalone binaries (separate release channel, ~1-2 weeks CI matrix).
- Plugin walker lazy-loading (~11s).
- Service-status refresh parallelisation (~20s post-startup-complete).
- mypyc / Cython for hot paths (low ROI — pydantic V2 already Rust, httpx/aiohttp already C).
- swc / stc TypeScript checker (not production-grade in 2026).
