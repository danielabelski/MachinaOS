import { useEffect, useMemo, useState } from 'react';
import { create } from 'zustand';
import { useWorkflowControlStore } from './workflowControlStore';
import type { WorkflowControlStatus } from '@/contexts/WebSocketContext';

const STAGES = ['preparation', 'authentication', 'hydration', 'routing', 'client_acquisition', 'client_lock',
  'client_construction', 'request_encoding', 'sdk_request', 'normalization', 'delivery', 'client_release', 'persistence', 'retry_wait'] as const;
type Stage = typeof STAGES[number];
export interface LlmWait {
  execution_key: string; request_id: string; seq: number; state: 'waiting' | 'cleared';
  stage: Stage; elapsed_ms: number; runtime_behavior_version: 1;
  scope: 'generation' | 'workspace'; execution_id: string; root_execution_id: string;
  generation?: number; workspace_epoch?: number; invocation_id?: string; thread_id?: string;
  turn_id?: string; agent_node_id?: string; agent_label?: string; activity_id?: string; attempt?: number;
  provider?: string; model?: string;
}
interface Entry extends LlmWait { node_id: string; observed_at: number }
type Waits = Record<string, Entry>;
const EMPTY: Waits = Object.freeze({});
const MAX_WORKFLOWS = 64;
const MAX_REQUESTS = 4096;
const keyOf = (wait: LlmWait) => JSON.stringify([wait.execution_key, wait.request_id]);
const boundedText = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 256;
const nonnegative = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

export function waitIsCurrent(wait: LlmWait, control?: WorkflowControlStatus): boolean {
  if (!control) return true;
  if (wait.scope === 'workspace') return !(wait.workspace_epoch != null && control.workspace_epoch != null && wait.workspace_epoch < control.workspace_epoch);
  if (wait.generation != null && control.generation != null && wait.generation < control.generation) return false;
  return !(control.root_execution_id && wait.root_execution_id && control.root_execution_id !== wait.root_execution_id);
}

export function parseLlmWait(value: unknown): LlmWait | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  if (raw.runtime_behavior_version !== 1 || !nonnegative(raw.seq) || raw.seq < 1 || !nonnegative(raw.elapsed_ms)
    || !['waiting', 'cleared'].includes(String(raw.state)) || !['generation', 'workspace'].includes(String(raw.scope))
    || !STAGES.includes(raw.stage as Stage)) return null;
  const result: Record<string, unknown> = {};
  for (const key of ['execution_key', 'request_id', 'execution_id', 'root_execution_id']) {
    if (!boundedText(raw[key])) return null;
    result[key] = raw[key];
  }
  for (const key of ['runtime_behavior_version', 'seq', 'elapsed_ms', 'stage', 'state', 'scope']) result[key] = raw[key];
  for (const key of ['invocation_id', 'thread_id', 'turn_id', 'agent_node_id', 'agent_label', 'activity_id', 'provider', 'model']) {
    if (!(key in raw)) continue;
    if (!boundedText(raw[key])) return null;
    result[key] = raw[key];
  }
  for (const key of ['generation', 'workspace_epoch', 'attempt']) {
    if (!(key in raw)) continue;
    if (!nonnegative(raw[key]) || (key === 'attempt' && raw[key] === 0)) return null;
    result[key] = raw[key];
  }
  return result as unknown as LlmWait;
}

interface WaitState {
  byWorkflow: Record<string, Waits>;
  closed: Record<string, Record<string, true>>;
  apply: (workflowId: string, nodeId: string, wait: LlmWait) => void;
  closeExecution: (workflowId: string, executionKey: string) => void;
  resetSession: () => void;
}
export const useLlmWaitStore = create<WaitState>((set) => ({
  byWorkflow: {}, closed: {},
  apply: (workflowId, nodeId, wait) => set((state) => {
    if (!boundedText(workflowId) || !boundedText(nodeId) || !parseLlmWait(wait) || state.closed[workflowId]?.[wait.execution_key]) return state;
    const slot = state.byWorkflow[workflowId] ?? EMPTY;
    const key = keyOf(wait);
    const previous = slot[key];
    if (previous && (previous.seq >= wait.seq || previous.state === 'cleared')) return state;
    // Never evict open requests or terminal tombstones. At the bounded
    // diagnostics limit, execution continues without admitting new notes.
    if (!previous && (Object.keys(slot).length >= MAX_REQUESTS
      || (!(workflowId in state.byWorkflow) && Object.keys(state.byWorkflow).length >= MAX_WORKFLOWS))) return state;
    const now = performance.now();
    const elapsed = Math.max(wait.elapsed_ms, previous ? previous.elapsed_ms + Math.max(0, now - previous.observed_at) : 0);
    return { byWorkflow: { ...state.byWorkflow, [workflowId]: { ...slot,
      [key]: { ...wait, elapsed_ms: elapsed, node_id: nodeId, observed_at: now } } } };
  }),
  closeExecution: (workflowId, executionKey) => set((state) => {
    if (!boundedText(workflowId) || !boundedText(executionKey) || state.closed[workflowId]?.[executionKey]
      || Object.keys(state.closed[workflowId] ?? EMPTY).length >= MAX_REQUESTS
      || (!(workflowId in state.closed) && Object.keys(state.closed).length >= MAX_WORKFLOWS)) return state;
    return { closed: { ...state.closed, [workflowId]: { ...state.closed[workflowId], [executionKey]: true } } };
  }),
  resetSession: () => set({ byWorkflow: {}, closed: {} }),
}));

const LABELS: Record<Stage, string> = {
  preparation: 'Preparing the model step', authentication: 'Preparing model authentication', hydration: 'Loading model inputs',
  routing: 'Selecting the model route', client_acquisition: 'Preparing the model client', client_lock: 'Waiting for the model client',
  client_construction: 'Preparing the model client', request_encoding: 'Preparing the model request',
  sdk_request: 'Waiting for the model response', normalization: 'Processing the model response', delivery: 'Delivering the model response',
  client_release: 'Finishing the model request', persistence: 'Saving the model step', retry_wait: 'Waiting to retry the model step',
};

export function useLlmWaitDisplay(workflowId: string | undefined, nodeId?: string) {
  const control = useWorkflowControlStore((state) => workflowId ? state.statuses[workflowId] : undefined);
  const slot = useLlmWaitStore((state) => workflowId ? state.byWorkflow[workflowId] ?? EMPTY : EMPTY);
  const closed = useLlmWaitStore((state) => workflowId ? state.closed[workflowId] : undefined);
  const waits = useMemo(() => Object.values(slot).filter((wait) => wait.state === 'waiting' && !closed?.[wait.execution_key]
    && waitIsCurrent(wait, control) && (!nodeId || wait.node_id === nodeId))
    .sort((a, b) => (b.elapsed_ms - b.observed_at) - (a.elapsed_ms - a.observed_at) || a.request_id.localeCompare(b.request_id)), [slot, closed, control, nodeId]);
  const wait = waits[0];
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!wait) return;
    const timer = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(timer);
  }, [wait]);
  if (!wait) return null;
  const provider = [wait.provider, wait.model].filter(Boolean).join(' / ');
  const text = `${nodeId ? '' : `${wait.agent_label || 'Agent'}: `}${LABELS[wait.stage]}${provider ? ` (${provider})` : ''}${waits.length > 1 ? ` · ${waits.length} requests` : ''}`;
  return { text, elapsedSeconds: Math.max(5, Math.floor((wait.elapsed_ms + Math.max(0, now - wait.observed_at)) / 1000)) };
}
/** Agent cards announce stage changes, never each clock tick. */
export function useLlmWaitNote(workflowId: string | undefined, nodeId?: string): string | null {
  return useLlmWaitDisplay(workflowId, nodeId)?.text ?? null;
}
