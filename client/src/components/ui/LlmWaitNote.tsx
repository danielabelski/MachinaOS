import { useLlmWaitDisplay } from '@/stores/llmWaitStore';
import { cn } from '@/lib/utils';

export function LlmWaitNote({ workflowId, className }: { workflowId?: string; className?: string }) {
  const note = useLlmWaitDisplay(workflowId);
  return note ? <p className={cn('m-0 min-w-0 break-words text-xs text-warning', className)}>
    <span role="status">{note.text}</span><span aria-hidden="true"> · {note.elapsedSeconds}s</span>
  </p> : null;
}
