import { describe, it, expect } from 'vitest';
import { sanitizeParameters } from '../parameterSanitizer';

/**
 * The regression these tests exist for: the runtime denylist held the
 * camelCase `memoryContent` while the backend field is `memory_content`, so the
 * exact-match check never fired and every export carried the Simple Memory
 * node's full conversation history.
 *
 * Names asserted here are the *backend* field names. If one is renamed in the
 * Pydantic model, this file should fail.
 */
describe('sanitizeParameters', () => {
  describe('simpleMemory runtime state', () => {
    it('strips the conversation history', () => {
      const out = sanitizeParameters({
        memory_content: '# Conversation History\n\n### **Human**\nmy api token is hunter2',
        window_size: 100,
      });
      expect(out).not.toHaveProperty('memory_content');
      expect(out.window_size).toBe(100);
    });

    it('strips the last Claude session id', () => {
      const out = sanitizeParameters({
        last_session_id: '45dbf1ac-1221-495d-9207-61b3559713ee',
      });
      expect(out).not.toHaveProperty('last_session_id');
    });

    it('strips the structured JSONL transcript behind the markdown view', () => {
      const out = sanitizeParameters({
        memory_jsonl: '{"role":"user","content":"my card is 4111 1111 1111 1111"}',
      });
      expect(out).toEqual({});
    });

    it('strips vertex cloud-conversation handles', () => {
      // These point into the *author's* Vertex session; an exported workflow
      // must not carry them to whoever imports it.
      const out = sanitizeParameters({
        vertex_interaction_id: 'interactions/abc123',
        vertex_environment_id: 'environments/xyz789',
        provider: 'vertex',
      });
      expect(out).toEqual({ provider: 'vertex' });
    });

    it('keeps genuine configuration on the same node', () => {
      const out = sanitizeParameters({
        memory_content: 'secrets in here',
        session_id: '',
        window_size: 50,
        long_term_enabled: true,
        retrieval_count: 3,
        embedding_provider: 'huggingface',
        embedding_endpoint: 'http://localhost:11434',
      });
      expect(out).toEqual({
        session_id: '',
        window_size: 50,
        long_term_enabled: true,
        retrieval_count: 3,
        embedding_provider: 'huggingface',
        embedding_endpoint: 'http://localhost:11434',
      });
    });
  });

  describe('spelling can no longer disable a filter', () => {
    it.each([
      'memory_content',
      'memoryContent',
      'MemoryContent',
      'MEMORY_CONTENT',
      'memory-content',
    ])('strips %s', key => {
      expect(sanitizeParameters({ [key]: 'history' })).toEqual({});
    });

    it.each(['api_key', 'apiKey', 'apikey', 'API_KEY'])('strips %s', key => {
      expect(sanitizeParameters({ [key]: 'sk-live-1234' })).toEqual({});
    });

    it.each(['access_token', 'accessToken', 'refresh_token', 'refreshToken'])(
      'strips %s',
      key => {
        expect(sanitizeParameters({ [key]: 'ya29.abc' })).toEqual({});
      },
    );
  });

  describe('credentials', () => {
    it('strips exact and substring credential keys', () => {
      const out = sanitizeParameters({
        password: 'p',
        client_secret: 's',
        private_key: 'k',
        cloudflare_api_token: 't',
        some_auth_token: 't',
        keep_me: 'ok',
      });
      expect(out).toEqual({ keep_me: 'ok' });
    });

    it("strips a webhook trigger's expected header value but keeps its header name", () => {
      const out = sanitizeParameters({
        path: 'orders',
        authentication: 'header',
        header_name: 'X-API-Key',
        header_value: 'the-shared-secret',
      });
      expect(out).toEqual({ path: 'orders', authentication: 'header', header_name: 'X-API-Key' });
    });

    it('keeps cloud_tool_key, which is an identifier and not a credential', () => {
      // A blanket `endsWith("key")` rule would discard this identifier
      // when exporting legacy workflow configuration.
      const out = sanitizeParameters({
        cloud_tool_key: 'type:web_search',
        label: 'Web Search',
      });
      expect(out).toEqual({ cloud_tool_key: 'type:web_search', label: 'Web Search' });
    });

    it('keeps other real param names that merely look credential-ish', () => {
      const out = sanitizeParameters({
        keyword: 'pizza',
        keywords: ['a', 'b'],
        authentication: 'none',
        allow_unauthenticated: true,
        allowed_credentials: ['openai'],
        include_author: true,
      });
      expect(Object.keys(out).sort()).toEqual([
        'allow_unauthenticated',
        'allowed_credentials',
        'authentication',
        'include_author',
        'keyword',
        'keywords',
      ]);
    });

    it('does not fire on token-shaped but harmless names', () => {
      const out = sanitizeParameters({
        max_tokens: 4096,
        maxTokens: 4096,
        budget_tokens: 1024,
        page_token: 'abc',
        nextPageToken: 'def',
        total_tokens: 10,
        input_tokens: 4,
        output_tokens: 6,
      });
      expect(Object.keys(out).sort()).toEqual(
        [
          'budget_tokens',
          'input_tokens',
          'maxTokens',
          'max_tokens',
          'nextPageToken',
          'output_tokens',
          'page_token',
          'total_tokens',
        ].sort(),
      );
    });
  });

  describe('traversal', () => {
    it('recurses into nested objects', () => {
      const out = sanitizeParameters({
        outer: { api_key: 'sk-1', keep: 1, deeper: { memory_content: 'x', ok: 2 } },
      });
      expect(out).toEqual({ outer: { keep: 1, deeper: { ok: 2 } } });
    });

    it('drops a nested object that becomes empty', () => {
      expect(sanitizeParameters({ creds: { api_key: 'sk-1' } })).toEqual({});
    });

    it('passes arrays through unchanged', () => {
      const out = sanitizeParameters({ tasks: [{ api_key: 'sk-1' }] });
      expect(out.tasks).toEqual([{ api_key: 'sk-1' }]);
    });

    it('preserves falsy values that are real configuration', () => {
      const out = sanitizeParameters({ enabled: false, count: 0, note: '' });
      expect(out).toEqual({ enabled: false, count: 0, note: '' });
    });
  });
});
