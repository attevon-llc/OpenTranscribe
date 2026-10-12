/**
 * What to tell the user after a change to their saved ASR providers (issues #1226, #1236).
 *
 * The server answers delete / activate / clear / create with the pipeline it leaves:
 * `diarizer_source_reverted`, `auto_activated` and `pipeline_issues` (stable codes). This only
 * turns those into messages keyed by code; the sentences are fixed and translated, and raw
 * server text is used for no code this build knows.
 */
import type { PipelineIssue, PipelineSeverity } from '$lib/api/pipeline';
import { isPipelineIssueCode, issueKey } from './pipelineIssues';

export interface ChangeMessage {
  severity: PipelineSeverity;
  /** Copy for a known code; null otherwise. */
  key: string | null;
  /** The server's fixed sentence, only for a code with no copy. */
  fallbackText: string | null;
}

// Standing facts about the engine or the deployment, true before and after any provider
// change; reporting them on every switch would be noise.
const STANDING: ReadonlySet<string> = new Set(['speaker_hints_ignored', 'source_locked']);

const ORDER: Record<PipelineSeverity, number> = { error: 0, warning: 1, info: 2 };

function issueMessages(issues: PipelineIssue[] | undefined): ChangeMessage[] {
  return (issues ?? [])
    .filter((i) => i.severity !== 'info' && !STANDING.has(i.code))
    .sort((a, b) => ORDER[a.severity] - ORDER[b.severity])
    .map((i) =>
      isPipelineIssueCode(i.code)
        ? { severity: i.severity, key: issueKey(i.code), fallbackText: null }
        : { severity: i.severity, key: null, fallbackText: i.message }
    );
}

/** After deleting a config, or switching the active one. */
export function changeMessages(result: {
  diarizer_source_reverted?: boolean;
  pipeline_issues?: PipelineIssue[];
}): ChangeMessage[] {
  const messages: ChangeMessage[] = [];
  if (result.diarizer_source_reverted) {
    messages.push({
      severity: 'info',
      key: 'settings.asrProvider.diarizerReverted',
      fallbackText: null,
    });
  }
  return [...messages, ...issueMessages(result.pipeline_issues)];
}

/** After creating a config: a FIRST one is auto-activated only when speaker detection keeps working. */
export function notAutoActivatedMessages(
  result: { auto_activated: boolean; pipeline_issues: PipelineIssue[] },
  wasFirstConfig: boolean
): ChangeMessage[] {
  if (result.auto_activated || !wasFirstConfig) return [];
  return [
    { severity: 'warning', key: 'settings.asrProvider.notAutoActivated', fallbackText: null },
    ...issueMessages(result.pipeline_issues),
  ];
}
