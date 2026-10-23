# Browser Skill

Use the connected private browser profile. It starts with fresh website sign-ins; use the existing Take control and Hand back flow for login, MFA and challenges.

1. Read the tool's trusted readiness note. If observation is required, call `snapshot` before navigation or another page-changing action.
2. Navigate to the intended URL, then take a fresh snapshot. Act only on current refs.
3. Observe again after a page-changing action. A stale ref or `next_action: snapshot` calls for a fresh snapshot, not repeated guesses or an automatic human handoff.
4. Verify the outcome before continuing. An uncertain purchase, message or other irreversible action requires inspection; do not repeat it blindly.

Keep rate limits, repeat limits, approval rules, protected-login observation suppression and human takeover intact. Never bypass a control latch.

The browser runs on its registered owner machine. `localhost` names that machine: a development server on another worker needs an explicitly reachable address. Shared workspace files do not establish network connectivity.

A five-second model-wait notice reports elapsed time, not failure. Wait for the current result; the notice does not authorize repeating an action.
