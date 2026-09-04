/**
 * Contract assertions for INodeTypeDescription values.
 *
 * Used by the adapter sweep in ``adapters/__tests__/nodeSpecToDescription.test.ts``
 * — each synthetic ``NodeSpec`` is converted via the adapter and the
 * resulting description must satisfy these invariants. A refactor that
 * drops ``displayName``, mangles ``displayOptions.show``, or uses the
 * wrong handle naming fails loudly.
 *
 * The legacy ``validateRegistry`` entry point (which iterated the old
 * ``nodeDefinitions/`` registry) was removed when the frontend registry
 * was deleted in favour of the backend NodeSpec path.
 */

import type {
  INodeTypeDescription,
  INodeProperties,
  INodeInputDefinition,
  INodeOutputDefinition,
} from '../types/INodeProperties';

export interface ContractIssue {
  nodeType: string;
  path: string;
  message: string;
}

// Wave 10.B: ``icon`` is backend-owned (served via NodeSpec from
// server/models/node_metadata.NODE_METADATA and assembled in
// services/node_spec.get_node_spec). The frontend type declares it as
// ``icon?: string`` — do NOT add ``icon`` back to this required list;
// the source of truth is the backend. See
// docs-internal/ARCHIVE/schema_source_of_truth_rfc.md.
const REQUIRED_TOP_LEVEL: (keyof INodeTypeDescription)[] = [
  'displayName',
  'name',
  'group',
  'version',
  'description',
  'defaults',
  'inputs',
  'outputs',
  'properties',
];

const VALID_PROPERTY_TYPES = new Set([
  'string',
  'number',
  'boolean',
  'options',
  'multiOptions',
  'collection',
  'fixedCollection',
  'json',
  'dateTime',
  'color',
  'hidden',
  'notice',
  'credentialsSelect',
  'resourceLocator',
  'slider',
  'file',
  'array',
  'code',
]);

export function validateNodeDefinition(
  nodeKey: string,
  def: INodeTypeDescription,
): ContractIssue[] {
  const issues: ContractIssue[] = [];
  const push = (path: string, message: string): void => {
    issues.push({ nodeType: nodeKey, path, message });
  };

  // 1. registry key must match definition.name
  if (def.name !== nodeKey) {
    push('name', `registry key "${nodeKey}" must equal definition.name "${def.name}"`);
  }

  // 2. required top-level fields
  for (const field of REQUIRED_TOP_LEVEL) {
    if (def[field] === undefined || def[field] === null) {
      push(String(field), `required field is missing`);
    }
  }

  // 3. group must be a non-empty array of strings
  if (def.group && (!Array.isArray(def.group) || def.group.length === 0)) {
    push('group', 'group must be a non-empty string[]');
  }

  // 4. defaults must contain `name`
  if (def.defaults && !def.defaults.name) {
    push('defaults.name', 'defaults.name is required');
  }

  // 5. inputs/outputs shape
  validateConnections('inputs', def.inputs, push);
  validateConnections('outputs', def.outputs, push);

  // 6. properties array
  if (Array.isArray(def.properties)) {
    const seen = new Set<string>();
    def.properties.forEach((prop, idx) => {
      validateProperty(prop, `properties[${idx}]`, push);
      if (prop?.name) {
        if (seen.has(prop.name)) {
          push(`properties[${idx}].name`, `duplicate property name "${prop.name}"`);
        }
        seen.add(prop.name);
      }
    });
  } else {
    push('properties', 'properties must be an array');
  }

  return issues;
}

function validateConnections(
  field: 'inputs' | 'outputs',
  value: string[] | INodeInputDefinition[] | INodeOutputDefinition[] | undefined,
  push: (path: string, message: string) => void,
): void {
  if (!Array.isArray(value)) {
    push(field, `${field} must be an array (string[] or definition objects[])`);
    return;
  }
  value.forEach((entry, idx) => {
    if (typeof entry === 'string') return; // legacy string form is allowed
    if (!entry || typeof entry !== 'object') {
      push(`${field}[${idx}]`, `entry must be string or object, got ${typeof entry}`);
      return;
    }
    const e = entry as INodeInputDefinition | INodeOutputDefinition;
    if (!e.name) push(`${field}[${idx}].name`, 'name is required');
    if (!e.type) push(`${field}[${idx}].type`, 'type is required');
  });
}

function validateProperty(
  prop: INodeProperties | undefined,
  pathPrefix: string,
  push: (path: string, message: string) => void,
): void {
  if (!prop || typeof prop !== 'object') {
    push(pathPrefix, 'property must be an object');
    return;
  }
  if (!prop.name) push(`${pathPrefix}.name`, 'name is required');
  if (!prop.displayName) push(`${pathPrefix}.displayName`, 'displayName is required');
  if (!prop.type) push(`${pathPrefix}.type`, 'type is required');
  if (prop.type && !VALID_PROPERTY_TYPES.has(prop.type)) {
    push(`${pathPrefix}.type`, `unknown property type "${prop.type}"`);
  }

  // displayOptions.show: every value must be array
  const show = prop.displayOptions?.show;
  if (show) {
    for (const [key, val] of Object.entries(show)) {
      if (!Array.isArray(val)) {
        push(
          `${pathPrefix}.displayOptions.show.${key}`,
          `displayOptions.show values must be arrays, got ${typeof val}`,
        );
      }
    }
  }

  // options-typed props should declare options array
  if ((prop.type === 'options' || prop.type === 'multiOptions') && !Array.isArray(prop.options)) {
    push(`${pathPrefix}.options`, `${prop.type} property must declare an options array`);
  }
}

export function formatIssues(issues: ContractIssue[]): string {
  if (issues.length === 0) return 'no issues';
  return issues
    .map((i) => `  - ${i.nodeType} :: ${i.path} - ${i.message}`)
    .join('\n');
}
