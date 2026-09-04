# Credentials Test Suite

Locks in the invariants enumerated in [docs-internal/ARCHIVE/credentials_panel.md §5](../../../docs-internal/ARCHIVE/credentials_panel.md).

## Run

```bash
cd server
uv pip install -e ".[test]"
uv run pytest tests/credentials/ -v

# Coverage report on the three critical modules
uv run pytest tests/credentials/ --cov=core.encryption --cov=core.credentials_database --cov=services.auth --cov-report=term-missing
```

## Layout

| File | Locks in |
|---|---|
| `test_encryption.py` | Invariant 13 — round-trip correctness, error paths, salt randomness |
| `test_credentials_database.py` | Invariants 3, 8 — two-table separation, encryption-at-rest, session/customer isolation |
| `test_auth_service.py` | Invariants 7, 8 — single point of access, memory cache behaviour, `clear_cache()` |
| `test_oauth_utils.py` | Invariant 12 — runtime redirect URI derivation (ws→http, wss→https) |
| `test_twitter_oauth.py` | Invariants 9, 10 — PKCE state single-use, code_challenge = base64url(sha256(verifier)) |
| `test_google_oauth.py` | Invariant 11 — `access_type=offline`, `prompt=consent` |
| `test_websocket_handlers.py` | Invariants 1, 5, 6 — WS message types, snake_case `has_key`, distinct provider-defaults handler |

## Fixtures

`conftest.py` provides:
- `encryption` / `uninitialized_encryption` — `EncryptionService` instances
- `credentials_db` — fresh on-disk SQLite per test in `tmp_path`, real Fernet
- `auth_service` — `AuthService` backed by the test DB

## What this suite does NOT test

- Real OAuth provider responses (`respx` mocks all upstream calls)
- The full FastAPI app boot (handlers are invoked directly)
- LangChain / native LLM SDK behaviour (covered by `tests/llm/`)
- Frontend `CredentialsModal` rendering (covered by `client/src/test/`)
- WhatsApp Go RPC binary
