/**
 * Tests for useAppStore.requestChatFocus and the authoritative graph a
 * save hands back.
 *
 * - requestChatFocus bumps a monotonic counter the console's Chat pane
 *   watches (components/__tests__/ConsolePanel.focus.test.tsx).
 * - A save's response replaces the edit buffer with the server's
 *   normalized graph and reconciles the selection.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// --- mocks (hoisted; closures dereference the spies at call time) -----------

const getAllWorkflowsMock = vi.fn();
const getWorkflowMock = vi.fn();
const saveWorkflowMock = vi.fn();
const deleteWorkflowMock = vi.fn();

vi.mock('../../services/workflowApi', () => ({
  workflowApi: {
    getAllWorkflows: (...args: unknown[]) => getAllWorkflowsMock(...args),
    getWorkflow: (...args: unknown[]) => getWorkflowMock(...args),
    saveWorkflow: (...args: unknown[]) => saveWorkflowMock(...args),
    deleteWorkflow: (...args: unknown[]) => deleteWorkflowMock(...args),
  },
}));

const toastInfoMock = vi.fn();
const toastErrorMock = vi.fn();
vi.mock('sonner', () => ({
  toast: {
    info: (...args: unknown[]) => toastInfoMock(...args),
    error: (...args: unknown[]) => toastErrorMock(...args),
    success: vi.fn(),
    message: vi.fn(),
    warning: vi.fn(),
  },
}));

import { useAppStore } from '../useAppStore';
import { createSavedGraphResource } from '../../services/agentCreationApi';

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({
    currentWorkflow: null,
    hasUnsavedChanges: false,
    selectedNode: null,
    consolePanelVisible: false,
    chatFocusRequest: 0,
  });
});

// ---------------------------------------------------------------------------

describe('useAppStore.requestChatFocus', () => {
  it('increments the monotonic chatFocusRequest counter', () => {
    expect(useAppStore.getState().chatFocusRequest).toBe(0);
    useAppStore.getState().requestChatFocus();
    expect(useAppStore.getState().chatFocusRequest).toBe(1);
    useAppStore.getState().requestChatFocus();
    expect(useAppStore.getState().chatFocusRequest).toBe(2);
  });
});

describe('useAppStore authoritative normalized graph handoff', () => {
  it('queues ordinary Save behind creation and saves the minted node', async () => {
    useAppStore.setState({ currentWorkflow: { id: 'queue', name: 'Queue', slug: 'Queue', nodes: [], edges: [], createdAt: new Date(), lastModified: new Date() } });
    saveWorkflowMock.mockImplementation(async (id, _name, data) => ({ id, slug: 'Queue', data }));
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const create = createSavedGraphResource('queue', () => {}, async () => {
      await pending;
      return { node_ids: { agent: 'queue:aiAgent:1' }, operations: [{ type: 'add_node', client_ref: 'agent', minted_id: 'queue:aiAgent:1', node_type: 'aiAgent', parameters: {}, label: 'Agent' }] };
    });
    await vi.waitFor(() => expect(saveWorkflowMock).toHaveBeenCalledOnce());
    const save = useAppStore.getState().saveWorkflow();
    expect(saveWorkflowMock).toHaveBeenCalledOnce();
    finish(); await create; await save;
    expect(saveWorkflowMock).toHaveBeenCalledTimes(2);
    expect(saveWorkflowMock.mock.calls[1][2].nodes.map((node: { id: string }) => node.id)).toContain('queue:aiAgent:1');
  });
  it('replaces the edit buffer with save response data and reconciles selection', async () => {
    const draftAgent = {
      id: 'draft-agent',
      type: 'aiAgent',
      data: { label: 'Agent' },
      position: { x: 100, y: 100 },
    };
    useAppStore.setState({
      currentWorkflow: {
        id: 'wf-1',
        name: 'Context flow',
        slug: 'Context_flow_1',
        nodes: [draftAgent],
        edges: [],
        createdAt: new Date(),
        lastModified: new Date(),
      },
      selectedNode: draftAgent,
      hasUnsavedChanges: true,
    });
    const normalizedAgent = { ...draftAgent, id: 'wf-1:aiAgent:1' };
    const contextNode = {
      id: 'wf-1:context:1',
      type: 'context',
      data: { systemManaged: true },
      position: { x: -120, y: 100 },
    };
    const contextEdge = {
      id: 'wf-1:edge:context:1',
      source: contextNode.id,
      target: normalizedAgent.id,
      sourceHandle: 'output-context',
      targetHandle: 'input-context',
    };
    saveWorkflowMock.mockResolvedValue({
      success: true,
      id: 'wf-1',
      slug: 'Context_flow_1',
      nodeIdAliases: { 'draft-agent': normalizedAgent.id },
      data: {
        graphVersion: 2,
        nodes: [normalizedAgent, contextNode],
        edges: [contextEdge],
      },
    });

    await useAppStore.getState().saveWorkflow();

    const state = useAppStore.getState();
    expect(state.currentWorkflow?.nodes).toEqual([normalizedAgent, contextNode]);
    expect(state.currentWorkflow?.edges).toEqual([contextEdge]);
    expect(state.selectedNode).toEqual(normalizedAgent);
    expect(state.hasUnsavedChanges).toBe(false);
  });

  it('uses aliases only as a compatibility fallback when save data is absent', async () => {
    const draftAgent = {
      id: 'draft-agent',
      type: 'aiAgent',
      data: {},
      position: { x: 0, y: 0 },
    };
    useAppStore.setState({
      currentWorkflow: {
        id: 'wf-1',
        name: 'Legacy save',
        slug: '',
        nodes: [draftAgent],
        edges: [],
        createdAt: new Date(),
        lastModified: new Date(),
      },
      selectedNode: draftAgent,
    });
    saveWorkflowMock.mockResolvedValue({
      success: true,
      id: 'wf-1',
      nodeIdAliases: { 'draft-agent': 'wf-1:aiAgent:1' },
    });

    await useAppStore.getState().saveWorkflow();

    expect(useAppStore.getState().currentWorkflow?.nodes[0].id).toBe(
      'wf-1:aiAgent:1',
    );
    expect(useAppStore.getState().selectedNode?.id).toBe('wf-1:aiAgent:1');
  });
});
