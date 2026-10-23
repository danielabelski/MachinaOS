import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { parseLlmWait, useLlmWaitDisplay, useLlmWaitNote, useLlmWaitStore, waitIsCurrent, type LlmWait } from '../llmWaitStore';
import { useWorkflowControlStore } from '../workflowControlStore';
import type { WorkflowControlStatus } from '@/contexts/WebSocketContext';

const wait = (extra: Partial<LlmWait> = {}): LlmWait => ({ execution_key: 'execution-1', request_id: 'request-1',
  seq: 1, state: 'waiting', stage: 'preparation', elapsed_ms: 5000, runtime_behavior_version: 1,
  scope: 'generation', execution_id: 'e1', root_execution_id: 'root', generation: 1, ...extra });
beforeEach(() => {
  useLlmWaitStore.setState({ byWorkflow: {}, closed: {} });
  useWorkflowControlStore.setState({ statuses: {}, pending: {} });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
it('retains clear-before-warning and terminal tombstones', () => {
  const store = useLlmWaitStore.getState();
  store.apply('w', 'a', wait({ state: 'cleared', seq: 2 }));
  store.apply('w', 'a', wait());
  store.apply('w', 'a', wait({ seq: 99 }));
  const { result } = renderHook(() => useLlmWaitNote('w'));
  expect(result.current).toBeNull();
  act(() => store.closeExecution('w', 'execution-1'));
  act(() => store.apply('w', 'a', wait({ seq: 3, request_id: 'late-request' })));
  expect(result.current).toBeNull();
});
it('retains terminal completion before any delayed warning arrives', () => {
  const store = useLlmWaitStore.getState();
  store.closeExecution('w', 'execution-1');
  store.apply('w', 'a', wait({ seq: 999 }));
  const { result } = renderHook(() => useLlmWaitNote('w'));
  expect(result.current).toBeNull();
});
it('aggregates delegated and concurrent requests without an old attempt clearing another', () => {
  const store = useLlmWaitStore.getState();
  store.apply('w', 'child', wait({ agent_label: 'Browser' }));
  store.apply('w', 'child', wait({ request_id: 'attempt-2', seq: 1, agent_label: 'Browser' }));
  const { result } = renderHook(() => useLlmWaitNote('w'));
  expect(result.current).toContain('Browser: Preparing the model step · 2 requests');
  act(() => store.apply('w', 'child', wait({ state: 'cleared', seq: 2 })));
  expect(result.current).toContain('Preparing the model step');
  expect(result.current).not.toContain('2 requests');
});
it('keeps Workspace tasks visible during a paused deployment and rejects archived epochs', () => {
  const control = { state: 'paused', root_execution_id: 'deployment', generation: 4, workspace_epoch: 3 } as WorkflowControlStatus;
  useWorkflowControlStore.setState({ statuses: { w: control } });
  expect(waitIsCurrent(wait({ generation: 1 }), control)).toBe(false);
  expect(waitIsCurrent(wait({ scope: 'workspace', workspace_epoch: 2 }), control)).toBe(false);
  useLlmWaitStore.getState().apply('w', 'a', wait({ scope: 'workspace', workspace_epoch: 3, stage: 'sdk_request' }));
  const { result } = renderHook(() => useLlmWaitNote('w'));
  expect(result.current).toContain('Waiting for the model response');
});
it('accepts only explicitly opted-in diagnostics', () => {
  expect(parseLlmWait({ ...wait(), runtime_behavior_version: 0 })).toBeNull();
  expect(parseLlmWait({ ...wait(), seq: NaN })).toBeNull();
  expect(parseLlmWait({ ...wait(), stage: 'invented' })).toBeNull();
  expect(parseLlmWait({ ...wait(), request_id: 'x'.repeat(257) })).toBeNull();
  expect(parseLlmWait({ ...wait(), seq: 0 })).toBeNull();
  expect(parseLlmWait(wait())).toEqual(wait());
});
it('advances elapsed time monotonically without changing the announcement or polling', () => {
  vi.useFakeTimers();
  useLlmWaitStore.getState().apply('w', 'a', wait({ stage: 'sdk_request', provider: 'gemini', model: 'fixture' }));
  const { result } = renderHook(() => useLlmWaitDisplay('w'));
  const announcement = result.current?.text;
  act(() => vi.advanceTimersByTime(242_000));
  expect(result.current?.elapsedSeconds).toBe(247);
  expect(result.current?.text).toBe(announcement);
  act(() => useLlmWaitStore.getState().apply('w', 'a', wait({ seq: 2, elapsed_ms: 5000, stage: 'persistence' })));
  expect(result.current?.elapsedSeconds).toBe(247);
  expect(result.current?.text).toContain('Saving the model step');
});
