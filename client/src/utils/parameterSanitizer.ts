/**
 * Parameter Sanitizer
 * Strips sensitive credentials and runtime state from node parameters before export.
 * Prevents API keys, tokens, passwords and conversation history from leaking into
 * exported workflow JSON.
 *
 * ## Every comparison here is normalization-insensitive, deliberately
 *
 * Parameter keys are defined by backend Pydantic models and cross the wire as
 * `snake_case` (see the naming table in CLAUDE.md). This file is TypeScript, so
 * it is very easy to write the `camelCase` spelling and produce a filter that
 * silently matches nothing.
 *
 * That is not hypothetical: `memoryContent` sat in the runtime list below while
 * the real field was `memory_content`, so `Set.has()` never matched and every
 * export carried the Simple Memory node's full conversation history. It reached
 * the shipped example workflows, and from there a public repository.
 *
 * So keys are compared after `normalizeKey`, which folds case and drops
 * separators. `memoryContent`, `memory_content` and `MEMORY-CONTENT` are all one
 * key. Listing a name in either spelling now works, and getting the spelling
 * wrong can no longer disable a filter.
 */

/** Fold case and drop separators so snake_case and camelCase collapse together. */
function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[_\-\s]/g, '');
}

const normalizedSet = (keys: string[]): Set<string> =>
  new Set(keys.map(normalizeKey));

// Parameter names that should never appear in exported JSON.
const SENSITIVE_EXACT_KEYS = normalizedSet([
  'api_key',
  'access_token',
  'refresh_token',
  'secret', 'password', 'passwd',
  'client_id', 'client_secret',
  'token', 'bearer_token',
  'private_key',
  'encryption_key',
  'oauth_token',
  'header_value', // webhookTrigger's expected authentication header
]);

// Legitimate parameter names that contain sensitive substrings but are NOT
// credentials. Checked first and always allowed through.
const SAFE_KEYS = normalizedSet([
  'max_tokens',
  'budget_tokens',
  'page_token', 'next_page_token',
  'token_count',
  'total_tokens',
  'input_tokens',
  'output_tokens',
]);

// Substring patterns - if a key contains any of these once normalized, the
// value is stripped. Specific enough to avoid firing on `maxTokens`/`pageToken`,
// which are additionally protected by SAFE_KEYS above.
const SENSITIVE_SUBSTRINGS = [
  'api_key',
  'secret',
  'password',
  'private_key',
  'access_token',
  'refresh_token',
  'bearer_token',
  'oauth_token',
  'auth_token',
].map(normalizeKey);

// Suffix rules, to catch vendor-prefixed credentials nobody has added yet
// (`bot_token`, `vercel_token`, ...) without enumerating every vendor.
//
// `token` singular only. Plural `tokens` is always a *count* in this codebase
// (`max_tokens`, `total_tokens`, `input_tokens`), so the singular suffix
// separates the two with no overlap.
//
// There is deliberately no `key` suffix rule. It reads plausible and it is
// wrong: fields such as `cloud_tool_key` can be stable identifiers rather
// than credentials, so stripping them would lose configuration on export.
// Credential-shaped `*_key` names are already
// covered by the `api_key` / `private_key` substrings above. Verified against
// every Params field on all registered plugins -- do not add `key` here
// without re-running that check.
const SENSITIVE_SUFFIXES = ['token'];

/**
 * Check if a parameter key holds sensitive credential data.
 * Returns true if the key should be stripped from exports.
 */
function isSensitiveKey(key: string): boolean {
  const normalized = normalizeKey(key);
  if (SAFE_KEYS.has(normalized)) return false;
  if (SENSITIVE_EXACT_KEYS.has(normalized)) return true;
  if (SENSITIVE_SUBSTRINGS.some(sub => normalized.includes(sub))) return true;
  return SENSITIVE_SUFFIXES.some(suffix => normalized.endsWith(suffix));
}

// Runtime/state keys that should not appear in exported workflows. These are
// transient execution state that happens to be persisted as a parameter, not
// user configuration, and several of them carry personal data.
//
// Names below are the backend field names. Do NOT add a key here on the basis
// of a `hidden: true` UI flag -- that marks merely-advanced configuration
// (`model`, `max_parallel`, `effort`), which an export legitimately needs.
// The first four mirror, exactly, what the backend's own "clear memory"
// routine resets or removes -- `clear_agent_session_state` in
// `server/services/memory/state.py`. That function is the authoritative
// definition of "everything an agent reuses across a conversation", so it is
// the right list to track, and a backend test asserts this file stays in sync
// with it.
const RUNTIME_KEYS = normalizedSet([
  // simpleMemory: the whole conversation, verbatim, including anything the
  // user or the model said.
  'memory_content',
  // simpleMemory: the structured JSONL transcript behind the markdown view.
  // No current writer, but older databases still carry it.
  'memory_jsonl',
  // simpleMemory: the Claude Code session UUID from the last successful run.
  'last_session_id',
  // Legacy Vertex handles into a cloud-side conversation and its
  // environment. Exporting them hands the recipient a pointer into the
  // author's own Vertex session.
  'vertex_interaction_id',
  'vertex_environment_id',
  'token_usage',           // Execution token metrics
  'execution_time',        // Runtime timing
  'last_execution',        // Last execution result
  'last_result',           // Cached result
]);

/** True if the key is runtime state rather than user configuration. */
function isRuntimeKey(key: string): boolean {
  return RUNTIME_KEYS.has(normalizeKey(key));
}

/**
 * Drop the top-level runtime keys and keep everything else, credentials
 * included. For copying a node within the same install: the copy keeps the
 * original's configuration but must not share its conversation or session.
 */
export function withoutRuntimeKeys(params: Record<string, any>): Record<string, any> {
  return Object.fromEntries(
    Object.entries(params).filter(([key]) => !isRuntimeKey(key)),
  );
}

/**
 * Deep-strip sensitive and runtime keys from a parameter object.
 * Returns a new object with sensitive values removed.
 * Recurses into nested objects but passes arrays through unchanged.
 */
export function sanitizeParameters(params: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};

  for (const [key, value] of Object.entries(params)) {
    // Skip sensitive credential keys
    if (isSensitiveKey(key)) continue;

    // Skip runtime/state keys
    if (isRuntimeKey(key)) continue;

    // Recurse into nested objects (but not arrays or null)
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const cleaned = sanitizeParameters(value);
      if (Object.keys(cleaned).length > 0) {
        result[key] = cleaned;
      }
    } else {
      result[key] = value;
    }
  }

  return result;
}
