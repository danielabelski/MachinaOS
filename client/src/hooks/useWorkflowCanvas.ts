import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useEdgesState, useNodesState } from 'reactflow';
import { useAppStore } from '../store/useAppStore';
import { sanitizeEdgesForComparison, sanitizeNodesForComparison } from '../utils/workflow';

/** Local ReactFlow edits belong to the exact workflow snapshot they came from. */
export function useWorkflowCanvas(debounceMs: number) {
  const workflow = useAppStore((state) => state.currentWorkflow);
  const [source, setSource] = useState(workflow);
  const [nodes, setNodes, onNodesChange] = useNodesState(workflow?.nodes ?? []);
  const [edges, setEdges, onEdgesChange] = useEdgesState(workflow?.edges ?? []);

  // Adopt a loaded/saved graph before committing a render. Hydrating in an
  // effect leaves an empty (or previous employee's) graph available to the
  // outgoing-state cleanup, including StrictMode's initial effect replay.
  if (source !== workflow) {
    setSource(workflow);
    setNodes(workflow?.nodes ?? []);
    setEdges(workflow?.edges ?? []);
  }

  const latest = useRef({ source, nodes, edges });
  useLayoutEffect(() => {
    latest.current = { source, nodes, edges };
  }, [source, nodes, edges]);

  const flush = useCallback(() => {
    const snapshot = latest.current;
    const store = useAppStore.getState();
    // A load may finish just before unmount, before React commits its new
    // graph. Never flush the outgoing editor into that newer store snapshot,
    // even when the new snapshot has the same workflow ID.
    if (!snapshot.source || store.currentWorkflow !== snapshot.source) return;
    const changed =
      JSON.stringify(sanitizeNodesForComparison(snapshot.nodes)) !== JSON.stringify(sanitizeNodesForComparison(snapshot.source.nodes))
      || JSON.stringify(sanitizeEdgesForComparison(snapshot.edges)) !== JSON.stringify(sanitizeEdgesForComparison(snapshot.source.edges));
    if (changed) store.updateWorkflow({ nodes: snapshot.nodes, edges: snapshot.edges });
  }, []);

  useEffect(() => {
    const timer = setTimeout(flush, debounceMs);
    return () => clearTimeout(timer);
  }, [source, nodes, edges, debounceMs, flush]);

  // Preserve edits made just before leaving Dev, without waiting for debounce.
  useEffect(() => flush, [flush]);

  return { nodes, setNodes, onNodesChange, edges, setEdges, onEdgesChange, flush };
}
