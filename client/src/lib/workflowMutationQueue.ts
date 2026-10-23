/** The saved graph has one writer per workflow, including ordinary Save. */
const queues = new Map<string, Promise<unknown>>();
export function serializeWorkflowMutation<T>(workflowId: string, mutate: () => Promise<T>): Promise<T> {
  const result = (queues.get(workflowId) ?? Promise.resolve()).then(mutate, mutate);
  queues.set(workflowId, result);
  void result.finally(() => { if (queues.get(workflowId) === result) queues.delete(workflowId); }).catch(() => {});
  return result;
}
