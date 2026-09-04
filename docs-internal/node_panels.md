# Node Parameter Panel — Logic Flow Documentation

> Reference for the three-section node configuration UI (Input / Parameters / Output).
> Companion test suite in [client/src/hooks/__tests__/](../client/src/hooks/__tests__) and
> [client/src/components/parameterPanel/__tests__/](../client/src/components/parameterPanel/__tests__).

The Parameter Panel is the modal that opens when a node is selected on the canvas. It has three
columns that can be hidden independently depending on the node type:

```
+---------------------------------------------------------------+
| header: icon + name + Run / Save / Cancel                     |
+----------------+--------------------------+-------------------+
| Input section  | Middle section            | Output section    |
| (left)         | (parameters / config)     | (right)           |
| flex 0.7       | flex 1.6                  | flex 0.7          |
+----------------+--------------------------+-------------------+
```

Files:
- [client/src/ParameterPanel.tsx](../client/src/ParameterPanel.tsx) — modal shell
- [client/src/components/parameterPanel/ParameterPanelLayout.tsx](../client/src/components/parameterPanel/ParameterPanelLayout.tsx) — flex layout
- [client/src/components/parameterPanel/InputSection.tsx](../client/src/components/parameterPanel/InputSection.tsx)
- [client/src/components/parameterPanel/MiddleSection.tsx](../client/src/components/parameterPanel/MiddleSection.tsx)
- [client/src/components/parameterPanel/OutputSection.tsx](../client/src/components/parameterPanel/OutputSection.tsx)
- [client/src/components/output/OutputPanel.tsx](../client/src/components/output/OutputPanel.tsx) — execution-results renderer (no edge walking; see §6)
- [client/src/utils/parameterVisibility.ts](../client/src/utils/parameterVisibility.ts) — `shouldShowParameter` (shared by MiddleSection + ParameterRenderer)
- [client/src/components/ParameterRenderer.tsx](../client/src/components/ParameterRenderer.tsx) — universal widget
- [client/src/hooks/useParameterPanel.ts](../client/src/hooks/useParameterPanel.ts)
- [client/src/hooks/useDragVariable.ts](../client/src/hooks/useDragVariable.ts)

## 1. Lifecycle

1. User clicks a node on the canvas → `selectedNode` set in Zustand store.
2. `ParameterPanel` mounts → `useParameterPanel()` fires.
3. Hook reads defaults from `nodeDefinition.properties[].default`, then asks backend for any saved
   parameters via WebSocket `get_node_parameters`. Saved values overlay defaults.
4. Modal renders three sections; `MiddleSection` filters parameters via `displayOptions.show`
   (see §4 invariants), then renders each visible parameter through `ParameterRenderer`.
5. User edits → `handleParameterChange(name, value)` updates local state. `hasUnsavedChanges`
   flips true (computed via `JSON.stringify` equality with original).
6. Save → WebSocket `save_node_parameters` → DB; on success `originalParameters` updated.
7. Run → if `hasUnsavedChanges` save first, then `executeNodeViaWebSocket`.
8. Cancel → revert pending edits, clear selection, close modal.

## 2. Section Visibility Rules

| Node type bucket | Input | Middle | Output |
|---|---|---|---|
| Start | hidden | shown | hidden |
| Skill (e.g. masterSkill, single skill nodes) | hidden | shown | hidden |
| Monitor (`teamMonitor`) | hidden | shown | hidden |
| Everything else | shown | shown | shown |

The buckets are not frontend type lists: `ParameterPanel.tsx` (around lines 141–150) reads
`hideInputSection` / `hideOutputSection` / `hideRunButton` straight off the node's backend
NodeSpec `uiHints` and passes the derived flags to `ParameterPanelLayout`. A plugin that wants the
Start / Skill / Monitor shape declares those hints itself (Wave 10.G.5); nothing in the client
matches on `node.type`.

## 3. Template Variable Naming (drag-and-drop contract)

When the user drags a value from `OutputPanel` into a parameter input, the dragged payload is a
template string `{{name.path}}` plus a JSON sidecar with metadata.

`name` is resolved by `useDragVariable.getTemplateVariableName(sourceNodeId)` with this strict
priority:

1. `node.data.label` — user-renamed label
2. `nodeDefinition.displayName` — built-in display name
3. `nodeType` — registered type name
4. `nodeId` — final fallback

In every case the result is **lowercased and whitespace-stripped** (`'My  Cron  Scheduler'` →
`'mycronscheduler'`).

The drag payload is set on both MIME types:
- `text/plain` → the template string `{{name.path}}` (used by simple text inputs)
- `application/json` → `{type: 'nodeVariable', nodeId, nodeName, key, variableTemplate, dataType}`

`effectAllowed` is `'copy'`.

### Drop discriminators

`ParameterRenderer.handleDrop` branches on `application/json`'s `type`. The three
have **different write semantics**, which is the whole reason they are separate —
reusing one for another's payload silently destroys user input.

| `type` | Produced by | Payload | Write semantics |
|---|---|---|---|
| `nodeVariable` | [`useDragVariable`](../client/src/hooks/useDragVariable.ts) (InputSection) | `{nodeId, nodeName, key, variableTemplate, dataType}` | **Appends** `variableTemplate` with smart spacing |
| `nodeOutput` | *nothing currently* — the branch exists but has no producer | `{value}` | **Replaces** unconditionally |
| `workspaceFile` | [`useDragWorkspaceFile`](../client/src/hooks/useDragWorkspaceFile.ts) (GalleryPanel) | `{path, ref}` — `ref` is a finished serialized `FileRef` from the server | **Conditional**: a `file` param takes `ref` whole; every other param **appends** `path` with smart spacing |

Two details that look incidental and are not:

- `workspaceFile` is deliberately **not** `nodeOutput`. That branch replaces the
  target value with no type check, so dropping a file into a half-written prompt
  would erase the prompt.
- Its `text/plain` fallback is the bare workspace-relative path, so a drop onto a
  plain `<textarea>` (or an editor outside the app) still lands something useful.
  Directories are non-draggable — the server sends them `ref: null`, and appending
  a bare folder path into a prompt was never the intent.

## 4. Parameter Visibility (`displayOptions.show`)

Each `INodeProperties` entry can include a `displayOptions.show` map. Values can be arrays
(allowed-values list) or scalars (single allowed value). All conditions must hold:

```ts
displayOptions: {
  show: {
    operation: ['create', 'update'],   // operation must be one of these
    useProxy: [true],                  // AND useProxy must be true
  }
}
```

When ALL conditions match the parameter renders; otherwise it's hidden.

A parameter without `displayOptions.show` always renders.

`shouldShowParameter` in [client/src/utils/parameterVisibility.ts](../client/src/utils/parameterVisibility.ts)
implements this. It was extracted out of `MiddleSection.tsx` so that `ParameterRenderer` (nested
`collection` options, ~line 760) can share it and so tests can import it directly —
[client/src/components/parameterPanel/__tests__/MiddleSection.test.tsx](../client/src/components/parameterPanel/__tests__/MiddleSection.test.tsx)
exercises the pure function (array membership, scalar equality, AND across keys, type-coercion
edges). `MiddleSection` applies it in its parameter filter (~line 187).

## 5. Connection Discovery (Input + Output)

`InputSection` walks the workflow's edges to figure out which other nodes feed the current one
(`OutputPanel` no longer does any edge walking — it only renders results). Its `isConfigHandle`
helper (~line 258) classifies handles into two buckets:

| Handle bucket | Examples | Effect |
|---|---|---|
| Data flow | `input-main`, `input-chat`, `input-task`, `input-teammates` | shown as connected nodes |
| Config / auxiliary | every other `input-*` — `input-context`, `input-tools`, `input-skill`, `input-model` | hidden on agents that declare `uiHints.hasSkills` — they belong to the dedicated UI in `MiddleSection` |

`input-memory` is retired (RFC-0002): `normalize_workflow_graph` rewrites legacy `simpleMemory ->
input-memory` edges into a Context node plus an ordinary `input-tools` edge, so the handle never
reaches this code on a normalised graph.

Plus a special case for **config nodes themselves**: a node is a config node when its NodeSpec
carries `uiHints.isConfigNode === true` (auto-derived on the backend by `_derive_auto_ui_hints` for
any plugin whose `group` contains `memory` or `tool`; `canvas` opts out explicitly). When the user is
viewing one, the panel inherits the parent agent's main inputs and labels them `via <Agent Name>` so
the user can still drag those upstream variables into the config node's parameters. The client never
inspects group strings for this.

## 6. Output Display (`OutputSection`)

`OutputSection` combines two sources of execution data:

1. `executionResults` — local results from in-modal Run button.
2. `nodeStatuses[selectedNode.id]` — push updates from workflow runs via WebSocket.

The WebSocket result is folded in at the front (newest-first) **only when** its `outputs` field
isn't already present in `executionResults` (deduplicated via `JSON.stringify`). Statuses other
than `success`/`error` (e.g. `running`) are ignored.

Rendering lives in [client/src/components/output/OutputPanel.tsx](../client/src/components/output/OutputPanel.tsx)
(the active renderer — `ui/OutputDisplayPanel.tsx` is legacy and unimported). The Response
section picks, in order: `response` / `output` / `text` / `content` (prose keys), then an
object-typed `result` (the canonical payload key CLI nodes fill with server-side-parsed JSON —
arrays survive `unwrap` un-peeled and surface here), then `stdout`. Objects/arrays render in the
themed `@uiw/react-json-view` tree. Strings render through ReactMarkdown **unless** the node's
NodeSpec declares `uiHints.outputMode = "terminal"` (CLI-wrapper plugins: `githubAction`,
`vercelAction`, `shell`) — then they render preformatted in a `<pre>` on the per-theme
`--code-*` surface, with wholly-JSON strings detected via the shared `tryParseJson`
(`utils/formatters.ts`) and routed to the tree instead. The panel resolves the spec via
`useNodeSpec(selectedNode?.type)` — backend owns display logic, no node-name checks.

## 7. Refactor Invariants

Locked in by the test suite at:
- [client/src/hooks/__tests__/useDragVariable.test.ts](../client/src/hooks/__tests__/useDragVariable.test.ts)
- [client/src/components/parameterPanel/__tests__/MiddleSection.test.tsx](../client/src/components/parameterPanel/__tests__/MiddleSection.test.tsx)
- [client/src/components/parameterPanel/__tests__/InputSection.test.tsx](../client/src/components/parameterPanel/__tests__/InputSection.test.tsx)
- [client/src/components/parameterPanel/__tests__/OutputSection.test.tsx](../client/src/components/parameterPanel/__tests__/OutputSection.test.tsx)

1. **Defaults loaded** from `nodeDefinition.properties[].default`; missing default ⇒ `null`.
2. **Saved params win** over defaults when merged (DB is source of truth).
3. **`hasUnsavedChanges`** is a deep-equal check against the original snapshot loaded from DB.
4. **Save** routes to `save_node_parameters` and updates the original snapshot on success.
5. **Cancel** restores the pending edits and clears `selectedNode`.
6. **Drag template variable** uses the priority `label > displayName > nodeType > nodeId`,
   normalised to lowercase + no whitespace.
7. **Drag payload** sets both `text/plain` and `application/json`; `effectAllowed = 'copy'`.
8. **`displayOptions.show`** hides a parameter unless ALL keyed conditions match. Array values
   are membership checks; scalar values are equality checks.
9. **Config handles** (`input-memory|tools|skill|model`) are SKIPPED by both `InputSection` and
   `OutputPanel` for agent nodes — those dependencies surface in MiddleSection.
10. **`input-main|chat|task|teammates`** are NEVER skipped — they are data flow.
11. **Memory / tool config nodes** inherit their parent agent's main inputs and label them
    `via <Agent Name>` so upstream variables remain draggable.
12. **OutputSection deduplication** compares result `outputs` via `JSON.stringify` before folding
    in WebSocket status into local results. Only `success` / `error` statuses fold in.

## 8. Run / Save / Cancel Buttons

- **Run** disabled while `isExecuting`. Prefixed by an autosave when `hasUnsavedChanges`.
- **Save** disabled when `!hasUnsavedChanges`.
- **Cancel/Stop** acts as Stop (cancels event-wait via WebSocket) when the node is in `waiting`
  state; otherwise plain Cancel that reverts edits and closes the modal.

## 9. Test Run

```bash
cd client
npm install
npm run test:run -- src/hooks/__tests__/useDragVariable.test.ts \
                    src/components/parameterPanel/__tests__
```

Or use the dedicated script (added in `client/package.json`):

```bash
npm run test:nodepanels
```
