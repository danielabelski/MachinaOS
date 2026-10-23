import { afterEach, expect, it, vi } from 'vitest';
import { createAgent, createSavedGraphResource } from '../agentCreationApi';
import { useAppStore } from '@/store/useAppStore';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it('retries one exact authoritative mutation after losing its acknowledgement', async () => {
  const fetch = vi.fn().mockRejectedValueOnce(new TypeError('lost')).mockResolvedValue(new Response(JSON.stringify({ operations: [], node_ids: {} })));
  vi.stubGlobal('fetch', fetch);
  await createAgent({ workflow_id: 'w', node_type: 'aiAgent', position: [10, 20] });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
});
it('retries a truncated success response with the same mutation identity', async () => {
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => { throw new TypeError('body interrupted'); } })
    .mockResolvedValueOnce(new Response(JSON.stringify({ operations: [], node_ids: {} })));
  vi.stubGlobal('fetch', fetch);
  await createAgent({ workflow_id: 'w', node_type: 'aiAgent', position: [10, 20] }, 'same-mutation');
  expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
});
it('serializes save/create/adopt and cancels creation after a workflow switch', async () => {
  const log: string[] = [];
  let finish!: () => void;
  const first = new Promise<void>((resolve) => { finish = resolve; });
  const state = { currentWorkflow: { id: 'w' }, saveWorkflow: vi.fn(async () => { log.push('save'); return true; }),
    adoptSavedOperations: vi.fn(() => log.push('adopt')) };
  vi.spyOn(useAppStore, 'getState').mockReturnValue(state as unknown as ReturnType<typeof useAppStore.getState>);
  const a = createSavedGraphResource('w', () => log.push('flush'), async () => { log.push('create'); await first; return { node_ids: {}, operations: [] }; });
  const b = createSavedGraphResource('w', () => log.push('flush-b'), async () => { log.push('create-b'); return { node_ids: {}, operations: [] }; });
  await vi.waitFor(() => expect(log).toEqual(['flush', 'save', 'create']));
  state.currentWorkflow = { id: 'other' };
  finish();
  await a; await b;
  expect(log).toEqual(['flush', 'save', 'create', 'adopt']);
});
