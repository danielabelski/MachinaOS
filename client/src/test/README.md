# Frontend Test Suite

Locks in the user-facing invariants from [docs-internal/ARCHIVE/credentials_panel.md §5](../../../docs-internal/ARCHIVE/credentials_panel.md).

## Run

```bash
cd client
npm install            # picks up the new devDependencies
npm run test           # watch mode
npm run test:run       # one-shot
npm run test:coverage  # with v8 coverage report
```

## Layout

| File | Locks in |
|---|---|
| `src/hooks/__tests__/useApiKeys.test.ts` | Hook routes to the correct WebSocket message types with the right payload shapes; failure paths return `{isValid: false, error}` without throwing |
| `src/components/__tests__/CredentialsModal.test.tsx` | Modal renders correctly for representative providers; click → WS message dispatch chain |

## Tooling

- **Vitest** + **jsdom** for fast component tests
- **@testing-library/react** + **@testing-library/user-event** for behaviour assertions
- **builders.ts** for test-data factories — keep test bodies focused on deltas, not boilerplate
- **setup.ts** stubs `matchMedia` / `ResizeObserver` / `IntersectionObserver` (jsdom doesn't ship them; antd needs them)

## What this suite does NOT test

- Antd internals (Modal, Form, Input, Select)
- `react-flow` rendering
- Backend handlers (covered by `server/tests/credentials/`)
- Full OAuth round-trip with real X / Google (no network)
