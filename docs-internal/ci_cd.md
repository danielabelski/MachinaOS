# CI/CD Pipeline

Internal reference for OpenCompany's GitHub Actions setup. Workflow inventory, release flow, and the composite setup action.

The repo currently ships **three workflows** plus one composite action. A number of hardening / security workflows described in earlier revisions of this doc (CodeQL, zizmor, rollback, reusable PyPI publish, cross-platform test-install) are **not yet implemented** — see [Planned (not yet implemented)](#planned-not-yet-implemented) at the end.

---

## Workflow inventory

```
                           +------------------+
  push to main ----------> |     ci.yml       |---+
  PR to main ------------> |                  |   |
                           +------------------+   |
                                                  |    +---------------------+
                                                  +--> |   predeploy.yml     |
                                                  |    |   (workflow_call)   |
  v*.*.* tag push -------> +------------------+   |    |                     |
  manual dispatch -------> |   release.yml    |---+    | - build-and-lint    |
                           |                  |        | - backend-tests     |
                           | predeploy gate,  |        | - cli-tests         |
                           | then publish     |        | - test-build-start  |
                           +------------------+        |   (3 OS)            |
                                  |                    +---------------------+
                                  +-- publish-npm
                                  +-- publish-github-packages
```

> Documentation is NOT deployed from this repo: the Mintlify docs live in
> the separate [zeenie-ai/docs-OpenCompany](https://github.com/zeenie-ai/docs-OpenCompany)
> repo and auto-deploy to docs.opencompany.sh via the Mintlify GitHub app
> (that repo's own workflow only runs `mintlify validate` + broken-links).

### Workflow summary

| Workflow | File | Triggers | Purpose |
|----------|------|----------|---------|
| CI | `.github/workflows/ci.yml` | push/PR → main | Delegates to predeploy.yml |
| Predeploy | `.github/workflows/predeploy.yml` | `workflow_call` | build/lint + backend tests + CLI tests + cross-OS build/start smoke |
| Release | `.github/workflows/release.yml` | `v*.*.*` tag, `workflow_dispatch` | predeploy gate → publish (npm + GitHub Packages) |
| Setup | `.github/actions/setup/action.yml` | (composite) | bun 1.4 + Node 22 + Python 3.12 + uv v8 + editable CLI install |

### Toolchain pin

- **`.python-version`** — single source of truth (`3.12`). The composite setup action currently hard-codes `python-version: '3.12'` in `setup-python` rather than reading the file via `python-version-file:`; keep the two values in sync when bumping.

### Required secrets

| Secret | Used by | Description |
|--------|---------|-------------|
| `NPM_TOKEN` | release.yml | npmjs publish access to the public `@zeenie-ai` scope (`publish-npm`) |
| `GITHUB_TOKEN` | release.yml | GitHub Packages publish (auto-provided) |

---

## Composite setup action

**File:** [.github/actions/setup/action.yml](../.github/actions/setup/action.yml)

```yaml
- uses: ./.github/actions/setup
- uses: ./.github/actions/setup
  with:
    node-version: '20'   # override Node only; Python is fixed at 3.12
```

| Tool | Version source | Action |
|------|---------------|--------|
| bun | `oven-sh/setup-bun` v2.2.0 — reads the root `packageManager` pin (`bun@1.4.0`) | Immutable commit SHA |
| Node.js | `node-version` input, default `22` | `actions/setup-node` v6.5.0, immutable commit SHA; package cache disabled |
| Python | hard-coded `3.12` (matches `.python-version`) | `actions/setup-python` v5.6.0, immutable commit SHA |
| uv | `astral-sh/setup-uv` v8.1.0, cache disabled | Immutable commit SHA |

`setup-node`'s `cache:` is intentionally omitted — its post-job cache save fails with a `Path Validation Error` in lanes that didn't run a JS install (CLI / backend test jobs), which would mark the whole job red even when every test passed. No dependency cache is configured for bun either (unchanged from the pnpm era).

After tool install the composite installs the supervisor CLI editably (`uv pip install --system -e .`).

---

## Predeploy validation

**File:** [.github/workflows/predeploy.yml](../.github/workflows/predeploy.yml)

Reusable `workflow_call` workflow with four independent jobs (no plan/change-detection gate, no aggregator — every job runs on every call):

- `build-and-lint` — `bun install --frozen-lockfile` + `bun run build`, then client lint (`bun run --filter react-flow-client lint`), TypeScript check (`... typecheck`), and frontend tests (`... test`, vitest). Runs on `ubuntu-latest`.
- `backend-tests` — `uv sync` + `uv run pytest tests/ -v` in `server/`. Whole suite, unsharded. Runs on `ubuntu-latest`.
- `cli-tests` — `uv pip install --system pytest pytest-asyncio pyyaml` + `python -m pytest cli/tests/ -v`. Runs on `ubuntu-latest`.
- `test-build-start` — cross-OS matrix (`ubuntu-latest`, `macos-latest`, `windows-latest`, `fail-fast: false`). Runs `bun run build`, then `bun run tsc --version` (proves the per-platform TypeScript 7 Go binary delivered via `optionalDependencies` resolves on every OS — the type-check gate itself runs on ubuntu only; `bun run`, never `bunx`, so it resolves strictly from the root `node_modules/.bin`), then a start smoke test. On Unix it backgrounds `bun run start`, reads `PYTHON_BACKEND_PORT` out of `.env.template` and polls `http://localhost:${APP_PORT}/health` for up to ~30 s, then `bun run stop`. On Windows it starts the supervisor as a background job, waits 15 s, and fails if the job already exited.

---

## Release workflow

**File:** [.github/workflows/release.yml](../.github/workflows/release.yml)

### Triggers

| Trigger | Behaviour |
|---------|-----------|
| Push of `v*.*.*` tag | predeploy gate, then publish to npm + GitHub Packages |
| `workflow_dispatch` | Same job graph (manual run; there is no dry-run switch) |

The workflow default is `contents: read`. Publishing permissions are scoped to the job that needs them: npm receives `id-token: write` for provenance, while GitHub Packages receives `packages: write`. Checkout credentials are not persisted in either publishing job.

### Job graph

```
predeploy (uses predeploy.yml)
   |
   +-------------------+
   v                   v
publish-npm                    publish-github-packages
   |                              |
   +- build                       +- build
   +- cli version sync            +- cli version sync
   +- verify npm auth             +- rewrite package name
   +- publish canonical package   +- publish mirror package
      @zeenie-ai/opencompany         @zeenie-ai/opencompany
      npmjs, public + provenance     npm.pkg.github.com
```

- Both publish jobs `needs: predeploy`, run on `ubuntu-latest`, and share the same prefix: immutable `actions/checkout` with `persist-credentials: false` → composite setup → immutable `actions/setup-node` (with the target `registry-url`) → `bun install --frozen-lockfile` → `bun run build` → `python -m cli version sync`.
- `publish-npm` — validates the token with `npm whoami`, then publishes the canonical public npmjs package `@zeenie-ai/opencompany` via `npm publish --access public --provenance` with `NODE_AUTH_TOKEN=secrets.NPM_TOKEN`. The `--provenance` flag emits an npm provenance attestation (backed by the workflow's `id-token: write`).
- `publish-github-packages` — rewrites `package.json` `name` to `@zeenie-ai/opencompany` and sets `publishConfig.registry = https://npm.pkg.github.com`, then `npm publish` with `NODE_AUTH_TOKEN=secrets.GITHUB_TOKEN`.

Both registries use `@zeenie-ai/opencompany`, matching the npm and GitHub
organization owned by the project.

There is currently no audit gate, no PyPI publish, no SLSA `attest-build-provenance` step, and no `create-github-release` / test-install stage — see [Planned](#planned-not-yet-implemented).

---

## Documentation deployment (separate repo)

The public Mintlify docs (docs.opencompany.sh) live in the separate
[zeenie-ai/docs-OpenCompany](https://github.com/zeenie-ai/docs-OpenCompany)
repo. Deployment happens automatically via the Mintlify GitHub app on
pushes to that repo's `main`; its own workflow only validates
(`mintlify validate` + broken-links). This repo previously carried a
`docs.yml` deploy workflow targeting an in-repo `docs-OpenCompany/`
folder — both the folder and the workflow are gone.

---

## Source files

| File | Purpose |
|------|---------|
| `.github/workflows/ci.yml` | CI entry point (delegates to predeploy.yml) |
| `.github/workflows/predeploy.yml` | Reusable validation (build/lint + backend tests + CLI tests + OS matrix build/start) |
| `.github/workflows/release.yml` | Tag / manual release: predeploy gate → publish npm + GitHub Packages |
| `.github/actions/setup/action.yml` | Composite: bun + Node + Python + uv + editable CLI install |
| `.github/dependabot.yml` | **Security updates only** — version-update PRs disabled; see below |
| `.python-version` | Toolchain pin (`3.12`) — single source of truth |

---

## Dependency update policy (security-only)

Dependabot raises **security PRs only**; routine version bumps are disabled
(`open-pull-requests-limit: 0` on every entry — the limit affects version
updates only; security PRs are exempt from both the limit and the schedule
and are grouped per ecosystem via `applies-to: security-updates`).

Three entries cover the real surfaces: `bun /` (bun workspace spans root +
`client/` + `server/nodejs/`), `pip /server` (authoritative
`server/pyproject.toml`; the committed `requirements.txt` export is the
transitive-pin surface security fixes patch), `github-actions /`. A former
`pip: /` entry (duplicate churn against `server/requirements.txt`) and a
dead `npm: /server` entry were removed.

**Caveat on the `bun` ecosystem**: Dependabot's `bun` support does version
updates only — it never raises security-update PRs, so the security-only
posture above yields no automated PRs at all for the JS workspace. Alerts
still fire; the remediation channel is the top-level `overrides` block in
the root `package.json` (the ranged pins formerly under `pnpm.overrides`).

Repo-level alerts + security updates must stay enabled — verify with
`gh api repos/zeenie-ai/OpenCompany/vulnerability-alerts` (204 = enabled).

Re-enable version updates deliberately, never by just deleting the limits:
raise `open-pull-requests-limit`, set `schedule.interval: monthly`, add
`groups` with `patterns: ["*"]` + `update-types: [minor, patch]`, and
`cooldown: {semver-major-days: 30}`. Stored comment-ignores (`mcp` 2.x,
`websockets` 17.x) are inspectable via `@dependabot show <dep> ignore
conditions`.

---

## Planned (not yet implemented)

The following existed in earlier drafts of this doc but are **not present in the current repo**. Listed here so the intent is preserved without misrepresenting the shipped pipeline:

- **`predeploy.yml` change-detection + aggregator** — a `plan` job (`dorny/paths-filter`) gating downstream jobs, a `pre-commit` job, pytest sharding by domain, and a `ci-passed` aggregator (`re-actors/alls-green`) as the single branch-protection target. Today every predeploy job runs unconditionally and there is no aggregator job.
- **`release.yml` hardening** — `workflow_dispatch` dry-run default, a once-per-release `build-for-publish` artifact, `actions/attest-build-provenance` SLSA attestations, and a `create-github-release` job. (An earlier draft also planned a blocking `pnpm audit`; bun has no audit-equivalent gate, so the top-level `overrides` block + Dependabot alerts are the vulnerability-remediation channel instead.)
- **`publish-pypi.yml`** — reusable PyPI publish (OIDC trusted publishing, `uv build --no-sources`, `pypa/gh-action-pypi-publish`). No PyPI distribution is published today.
- **`test-install.yml`** — cross-platform end-user install smoke (npm install, git clone, install script) across 3 OS.
- **`rollback.yml`** — manual `npm deprecate` + optional revert PR.
- **`codeql.yml`** — Python + JS/TS SAST (`security-extended`).
- **`check-zizmor.yml`** — workflow-security linter (SARIF to the Security tab).
- **`.pre-commit-config.yaml`** — ruff / prettier / eslint / actionlint hooks (note: the project rule is to verify with pytest + the root `typecheck` gate + eslint, not ruff). This file is **not currently present in the tree**; the entry describes intent, not a live hook.
