# OpenCompany Gateway

The hosted LLM gateway for OpenCompany: invited people use LLMs on platform-owned vendor keys, each with a monthly allowance, and spend is tracked per person and per session. It is the stock [LiteLLM Proxy](https://docs.litellm.ai/docs/) deployed with BerriAI's official Terraform module. Everything in this folder is configuration and tests; there is no custom gateway code.

| What | Where |
|---|---|
| Proxy config (models, budgets, cache) | [litellm/config.yaml](litellm/config.yaml) |
| AWS deployment (ECS Fargate, Aurora Postgres, ElastiCache, ALB) | [infra/terraform/](infra/terraform/) |
| Local stack with mock models | [local/](local/) |
| Contract and pin tests | [tests/](tests/) |
| CI | [.github/workflows/gateway-ci.yml](../.github/workflows/gateway-ci.yml) |
| How OpenCompany uses it | [docs-internal/llm_gateway.md](../docs-internal/llm_gateway.md) |

## How it works

- **People** are LiteLLM internal users. Each has a monthly USD allowance (`max_budget` with `budget_duration: 1mo`, reset on the 1st, UTC).
- **Keys**: each person creates one **Default**-type key per OpenCompany install in the console and pastes it into Connectors. Every key of a person draws on the same allowance. OpenCompany needs a Default key to show the allowance: LLM-API keys can't read `/user/info`.
- **Budgets** are checked before every call: LiteLLM reserves the worst-case cost in Redis, then settles the real cost. A spent allowance is refused with error type `budget_exceeded` (HTTP 429 on 1.103.x; 402 from 1.104, set by `budget_exceeded_status_code`). OpenCompany reads the type, not the status.
- **Prices** come from a pinned copy of LiteLLM's price list (`LITELLM_MODEL_COST_MAP_URL`). A model without a price is refused (HTTP 403, `model_cost_map_missing`) rather than billed at $0.
- **Attribution**: OpenCompany sends `x-litellm-session-id`, `x-litellm-tags` and `x-litellm-spend-logs-metadata`, so spend shows per person, per run and per workflow.
- **Retries**: OpenCompany sends an opt-in cache namespace on Temporal LLM steps (`cache: {"use-cache": true, "namespace": ...}`), so a retried call replays from Redis at $0.

## Served providers

| Kind | Providers |
|---|---|
| Chat | OpenAI, Anthropic, Gemini, Groq, Cerebras, xAI, Moonshot (Kimi), Mistral |
| Speech | OpenAI (speech and transcription) |
| Embeddings | OpenAI |

Gemini runs on Vertex AI express mode with a Google Cloud Agent Platform key (`GOOGLE_AGENT_API_KEY`, a key starting `AQ.`), billed to that key's project. The Gemini Developer API refuses these keys, so `litellm/config.yaml` points the `gemini` provider's `api_base` at `https://aiplatform.googleapis.com/v1/publishers/google`. Gemini is also OpenCompany's default model for installs that rely on the gateway (`"gateway"` in `server/config/llm_defaults.json`).

Not served: OpenRouter, DeepSeek and Sarvam (their default models have no price in the list), Groq speech (1.103.4 bills Groq transcription at $0), ElevenLabs and Deepgram (OpenCompany uses their native formats), and every tool API (Brave, Serper, TikHub, Google Maps, Perplexity, DeepL, Apify and the rest). Those stay on each install's own keys.

To serve another provider:
1. Add its secret name to `secret_env_names` and its wildcard to `litellm/config.yaml`.
2. Give it a `"gateway"` block in `server/config/llm_defaults.json` (chat), and `"gateway": true` in `server/config/speech_defaults.json` if its speech is served too.
3. Run the tests. `tests/test_pins.py` checks that its default models are priced; check its routes bill the person on the local stack.
4. Apply.

## Local stack and tests

Needs Docker. Mock models plus fake Anthropic, Gemini and OpenAI-audio upstreams ([local/fake_upstream.py](local/fake_upstream.py)) for what mocks can't show (spend rows on `/v1/messages`, the native Gemini route and the audio routes, streamed pricing): nothing is billed and no vendor key is needed. Three contract tests are strict expected failures that pin LiteLLM gaps OpenCompany works around. When an upgrade closes a gap, its test passes and fails the suite: lift the workaround and drop the marker.

```bash
docker compose -f gateway/local/docker-compose.yml up -d --wait
uv run --project gateway pytest gateway/tests -v
docker compose -f gateway/local/docker-compose.yml down -v
```

`tests/test_pins.py` needs no stack. It checks that:
- the version and price-list pins agree across files;
- every served provider's default model is priced;
- every vendor key the config reads has a Terraform secret;
- Gemini goes to Vertex with the Agent Platform key.

To try real Gemini on the local stack, mount a copy of `local/config.local.yaml` whose `gemini-*` and `gemini/*` entries match `litellm/config.yaml`, through a compose override that reads `GOOGLE_AGENT_API_KEY` from an env file outside the repository. That costs a fraction of a cent per call, billed to the key's project. The console runs at http://127.0.0.1:4000/ui (user `admin`, password `sk-local-master-key`).

## First deployment

Prerequisites: AWS credentials for the target account, an S3 bucket for Terraform state (fill `REPLACE_WITH_STATE_BUCKET` in `infra/terraform/envs/<env>.backend.hcl`), Terraform 1.14, and the `aws` CLI (the module runs the database bootstrap and migrations through it).

1. **DNS, once for both environments.** `opencompany.sh` is on Cloudflare, so delegate a subzone to Route 53:

   ```bash
   aws route53 create-hosted-zone --name gw.opencompany.sh --caller-reference "gw-$(date +%s)"
   ```

   Add the four NS records it returns in Cloudflare as `gw` NS records (DNS only). Wait until `dig NS gw.opencompany.sh +short` returns them, then put the zone id in `infra/terraform/envs/<env>.tfvars` (`REPLACE_WITH_GW_ZONE_ID`). Hostnames: `api.gw.opencompany.sh` (prod) and `staging.gw.opencompany.sh`.

2. **Secrets.** Create the empty secrets, then fill every one, including `SLACK_WEBHOOK_URL`: ECS tasks only start once each secret has a value.

   ```bash
   cd gateway/infra/terraform
   terraform init -backend-config=envs/staging.backend.hcl
   terraform apply -var-file=envs/staging.tfvars -target=aws_secretsmanager_secret.vendor
   aws secretsmanager put-secret-value --secret-id opencompany-gateway-staging/env/openai-api-key --secret-string 'sk-...'
   ```

   Give Gemini its own Agent Platform key: in the Google Cloud project, restricted to the Vertex AI API and bound to the project's service account. Create it in the console or with gcloud, then pass its string straight to the secret, so it never sits in a chat or a file:

   ```bash
   gcloud services api-keys create --display-name=opencompany-gateway \
     --api-target=service=aiplatform.googleapis.com --service-account=<service-account-email>
   aws secretsmanager put-secret-value --secret-id opencompany-gateway-staging/env/google-agent-api-key \
     --secret-string "$(gcloud services api-keys get-key-string <key-name> --format='value(keyString)')"
   ```

3. **Full apply.** `terraform apply -var-file=envs/staging.tfvars`. The module bootstraps the database and runs the migrations before the services start.

4. **SpendLogs indexes, once.** LiteLLM no longer creates them in migrations (1.103.3+). Run the command from `terraform output -raw spendlogs_index_command`; it reuses the module's bootstrap task with the index SQL.

5. **Price list check.** Call a current model through the gateway, for example `gpt-6-sol`. A 403 `model_cost_map_missing` means the task couldn't fetch the pinned price list at start and fell back to LiteLLM's bundled one: check the task logs, then restart the service.

6. **Admin access.**
   1. Read the master key from the `master_key_secret_arn` output and sign in to `<gateway_url>/ui` as `admin` with it.
   2. Create named admin accounts (Internal Users, role Admin) and sign in with one.
   3. Set `disable_env_credential_login: true` in `general_settings` and apply, so the master key no longer opens the console.

## Adding a person

1. In the console, create an internal user (or `POST /user/new`) with their monthly `max_budget`. New people default to `max_internal_user_budget` ($50).
2. Create an invitation (`POST /invitation/new`). Send them the link yourself: email delivery is an Enterprise feature. Links expire after 7 days.
3. They set a password, open **Virtual Keys**, and create one key per install with the **Default** key type (the console's default). They paste it into OpenCompany's Connectors panel.

Avoid team keys: they skip the person's allowance unless `apply_user_budget_to_team_keys` is set.

## Upgrades

LiteLLM ships a minor release every week and supports only the latest four minor lines, so upgrade every 2-4 weeks. Apply security releases within 72 hours ([advisories](https://github.com/BerriAI/litellm/security/advisories)).

1. Bump the version together in `local.litellm_version` and the module `version` (`infra/terraform/main.tf`), the image tag in `local/docker-compose.yml`, and `LITELLM_VERSION` in the CI workflow. `tests/test_pins.py` fails if they disagree.
2. To move the price list, pick a reviewed commit of `model_prices_and_context_window.json` and change `LITELLM_MODEL_COST_MAP_URL` in both `main.tf` and `local/docker-compose.yml`.
3. Open a PR: CI verifies the image signatures and runs the contract and pin tests on the new version.
4. Take an Aurora snapshot, then apply to staging. The module runs the database migration before the services roll.
5. Smoke-test staging with a real cheap model per served provider, then apply to prod.

Why 1.103.x: it is the only stable line with the streamed-alias billing fix (LiteLLM PR #44341, also in 1.106.0-dev). `test_streamed_alias_is_priced_at_the_deployment` guards streamed pricing through an alias on every upgrade. Moving to 1.104 or later also turns budget refusals into 402 (`budget_exceeded_status_code`).

## Operating notes

- **Alerts** go to Slack (`SLACK_WEBHOOK_URL`): budget thresholds, outages, database errors.
- **Spend logs** are kept for 90 days (`maximum_spend_logs_retention_period`); the daily rollups that drive the usage views are kept.
- **Redis** must never evict keys: evictions silently drop budget and rate-limit state. The module's ElastiCache cluster has a replica with automatic failover.
- **The salt key** (`LITELLM_SALT_KEY`) encrypts credentials stored in the database and can never change. Terraform generates it once and ignores later changes.
- **The price list** is fetched when a task starts. If GitHub is unreachable then, LiteLLM serves its bundled list (which refuses newer models) and retries in the background.
- **Rust**: LiteLLM's Rust path is beta and opt-in per model. To trial it, set `rust: true` on the Anthropic models in staging, confirm responses carry `x-litellm-rust: true` and spend logs are unchanged, then promote. The standalone Rust gateway is not production-ready yet.

## Known limits

- Replay covers completed LLM calls only; an abandoned stream is billed for what it produced.
- A small allowance refuses expensive models outright. LiteLLM reserves each call's worst case (the model's full context plus its maximum output) before admitting it, so an allowance below that refuses the model with `budget_exceeded`, even unspent. With $1, `gemini-3.5-flash`, `gemini-3.1-pro-preview` and `gemini-2.5-pro` were refused; the $50 default admits them.
- Gemini SDK calls (the native `generateContent` route) carry the session id and tags, but 1.103.4 drops their per-call metadata and doesn't replay them from the cache, so a retried Gemini step is billed again.
- `gpt-4o-mini-tts` (OpenCompany's Text to Speech default) carries a price override in `litellm/config.yaml`. The speech route returns no usage for the audio, so LiteLLM would otherwise bill the input text only, about 1% of the cost. The override's per-token rate assumes about 200 text tokens per minute of speech; revisit it if OpenAI's price changes.
- Groq speech is not served: 1.103.4 bills Groq transcription at $0. The `groq/*` wildcard still lets a person call Groq Whisper directly with their key, billed $0 to them; Groq charges about $0.11 per hour of audio.
- A Default key can create and delete that person's own keys, but not raise their allowance. Revoke a leaked key in the console.
- Claude Code on the gateway holds the install's gateway key in its process environment, where its own tools (Bash) can read it, as with any API key a CLI agent runs on. The allowance caps what a leaked key can spend.
- The open-source tier has no SSO beyond 5 users, no audit log, no automatic key rotation, and no route permissions for tool pass-through. Those need a LiteLLM Enterprise license.
