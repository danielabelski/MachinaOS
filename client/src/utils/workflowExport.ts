/**
 * Workflow Export/Import Utilities
 * Handles exporting workflows to JSON files and importing from JSON.
 * Node parameters are embedded in the exported JSON under a `nodeParameters` key,
 * with sensitive credentials (API keys, tokens) stripped before export.
 */

import { Node } from 'reactflow';
import { WorkflowData } from '../store/useAppStore';
import { validateWorkflow, serializeWorkflow, deserializeWorkflow } from '../schemas/workflowSchema';
import { sanitizeParameters } from './parameterSanitizer';
import { getCachedNodeSpec } from '../lib/nodeSpec';
// Injected by Vite define from root package.json (see vite.config.js)
declare const __APP_VERSION__: string;
const APP_VERSION = typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '0.0.0';

// Fields allowed in node.data for export/save - everything else is parameter data
// that belongs in the DB and should not leak into exported JSON files.
export const UI_DATA_FIELDS = new Set([
  'label',
  'disabled',
  'condition',
  // Backend-owned Context companion identity. The client only preserves
  // these values; normalization and lifecycle decisions remain server-side.
  'systemManaged',
  'agentNodeId',
  'private_browser_owner',
]);

/**
 * Declarative manifest of what an exported workflow needs to run on the
 * target instance. Populated from each node's cached NodeSpec at export time;
 * read by the import flow to surface a "missing credentials / unknown
 * plugin" prompt before the workflow is saved.
 *
 * - `credentials`: the set of credential provider IDs the workflow references
 *   (from each `NodeSpec.credentials`). Carries IDs only — values are stripped
 *   by `parameterSanitizer`.
 * - `nodes`: the plugin types and versions the workflow was authored against.
 *   Used informationally for drift detection; not enforced.
 *
 * Older exports without this field import cleanly — its presence is the
 * discriminator (no `format_version` bump needed).
 */
export interface RequirementsManifest {
  credentials: Array<{ provider_id: string }>;
  nodes: Array<{ type: string; version: number }>;
}

/** Workflow data with optional embedded node parameters (from new-format exports). */
export interface ImportedWorkflow extends WorkflowData {
  nodeParameters?: Record<string, Record<string, any>>;
  requirements?: RequirementsManifest;
}

/**
 * Build the per-export `requirements` manifest by walking each node's
 * cached NodeSpec. Returns undefined if no metadata is available
 * (cold-start before specs warm — rare).
 */
function buildRequirements(nodes: Node[]): RequirementsManifest | undefined {
  const credSet = new Set<string>();
  const nodeReqs: Array<{ type: string; version: number }> = [];
  for (const n of nodes) {
    const spec = getCachedNodeSpec(n.type ?? '');
    if (!spec) continue;
    nodeReqs.push({ type: spec.type, version: spec.version ?? 1 });
    for (const credId of spec.credentials ?? []) credSet.add(credId);
  }
  if (credSet.size === 0 && nodeReqs.length === 0) return undefined;
  return {
    credentials: [...credSet].map(provider_id => ({ provider_id })),
    nodes: nodeReqs,
  };
}

/**
 * Strip node.data down to UI-essential fields only.
 * Prevents personal data (phone numbers, contact names, API keys, etc.)
 * from leaking into exported workflow JSON files.
 */
export function sanitizeNodes(nodes: Node[]): Node[] {
  return nodes.map(node => ({
    ...node,
    data: Object.fromEntries(
      Object.entries(node.data || {}).filter(([key]) => UI_DATA_FIELDS.has(key))
    )
  }));
}

/**
 * Build the sanitized nodeParameters object for embedding in export JSON.
 * Strips sensitive credentials from each node's parameters.
 */
function buildExportParameters(
  nodeParameters?: Record<string, Record<string, any>>
): Record<string, Record<string, any>> | undefined {
  if (!nodeParameters || Object.keys(nodeParameters).length === 0) return undefined;

  const sanitized: Record<string, Record<string, any>> = {};
  for (const [nodeId, params] of Object.entries(nodeParameters)) {
    const cleaned = sanitizeParameters(params);
    if (Object.keys(cleaned).length > 0) {
      sanitized[nodeId] = cleaned;
    }
  }
  return Object.keys(sanitized).length > 0 ? sanitized : undefined;
}

/**
 * Export workflow to JSON file download.
 * @param nodeParameters - Optional map of node_id -> parameters fetched from DB
 */
export function exportWorkflowToFile(
  workflow: WorkflowData,
  nodeParameters?: Record<string, Record<string, any>>
): void {
  const validation = validateWorkflow(workflow);

  if (!validation.valid) {
    console.error('Workflow validation errors:', validation.errors);
    throw new Error(`Cannot export invalid workflow: ${validation.errors.join(', ')}`);
  }

  const workflowJSON: any = {
    ...workflow,
    nodes: sanitizeNodes(workflow.nodes),
    createdAt: workflow.createdAt.toISOString(),
    lastModified: workflow.lastModified.toISOString(),
    version: APP_VERSION
  };

  const exportParams = buildExportParameters(nodeParameters);
  if (exportParams) {
    workflowJSON.nodeParameters = exportParams;
  }

  const requirements = buildRequirements(workflow.nodes);
  if (requirements) {
    workflowJSON.requirements = requirements;
  }

  const jsonString = serializeWorkflow(workflowJSON);
  const blob = new Blob([jsonString], { type: 'application/json' });
  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');
  a.href = url;
  a.download = `${workflow.name || 'workflow'}_${workflow.id}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Import workflow from JSON file.
 * Extracts embedded nodeParameters if present (new format).
 */
export function importWorkflowFromFile(file: File): Promise<ImportedWorkflow> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = (event) => {
      try {
        const jsonString = event.target?.result as string;
        const raw = JSON.parse(jsonString);

        // Extract embedded nodeParameters + requirements manifest before
        // standard deserialization. Both are optional — older exports
        // without them import cleanly.
        const nodeParameters = raw.nodeParameters || undefined;
        const requirements: RequirementsManifest | undefined = raw.requirements || undefined;

        const workflow = deserializeWorkflow(jsonString);

        const validation = validateWorkflow(workflow);
        if (!validation.valid) {
          reject(new Error(`Invalid workflow JSON: ${validation.errors.join(', ')}`));
          return;
        }

        resolve({ ...workflow, nodeParameters, requirements });
      } catch (error) {
        reject(error);
      }
    };

    reader.onerror = () => {
      reject(new Error('Failed to read file'));
    };

    reader.readAsText(file);
  });
}

/**
 * Export workflow to JSON string.
 * @param nodeParameters - Optional map of node_id -> parameters fetched from DB
 */
export function exportWorkflowToJSON(
  workflow: WorkflowData,
  nodeParameters?: Record<string, Record<string, any>>
): string {
  const validation = validateWorkflow(workflow);

  if (!validation.valid) {
    throw new Error(`Cannot export invalid workflow: ${validation.errors.join(', ')}`);
  }

  const workflowJSON: any = {
    ...workflow,
    nodes: sanitizeNodes(workflow.nodes),
    createdAt: workflow.createdAt.toISOString(),
    lastModified: workflow.lastModified.toISOString(),
    version: APP_VERSION
  };

  const exportParams = buildExportParameters(nodeParameters);
  if (exportParams) {
    workflowJSON.nodeParameters = exportParams;
  }

  const requirements = buildRequirements(workflow.nodes);
  if (requirements) {
    workflowJSON.requirements = requirements;
  }

  return serializeWorkflow(workflowJSON);
}

/**
 * Import workflow from JSON string.
 * Extracts embedded nodeParameters if present (new format).
 */
export function importWorkflowFromJSON(jsonString: string): ImportedWorkflow {
  const raw = JSON.parse(jsonString);
  const nodeParameters = raw.nodeParameters || undefined;
  const requirements: RequirementsManifest | undefined = raw.requirements || undefined;

  const workflow = deserializeWorkflow(jsonString);

  const validation = validateWorkflow(workflow);
  if (!validation.valid) {
    throw new Error(`Invalid workflow JSON: ${validation.errors.join(', ')}`);
  }

  return { ...workflow, nodeParameters, requirements };
}

/**
 * Copy workflow to clipboard as JSON
 * @param nodeParameters - Optional map of node_id -> parameters fetched from DB
 */
export async function copyWorkflowToClipboard(
  workflow: WorkflowData,
  nodeParameters?: Record<string, Record<string, any>>
): Promise<void> {
  const jsonString = exportWorkflowToJSON(workflow, nodeParameters);
  await navigator.clipboard.writeText(jsonString);
}

/**
 * Paste workflow from clipboard
 */
export async function pasteWorkflowFromClipboard(): Promise<ImportedWorkflow> {
  const jsonString = await navigator.clipboard.readText();
  return importWorkflowFromJSON(jsonString);
}
