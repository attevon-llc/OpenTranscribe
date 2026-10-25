<script lang="ts">
  import { t } from '$stores/locale';
  import {
    isFinalJobState,
    type FinalJobState,
    type ProcessingJobModel,
  } from '$lib/notifications/processingJob';

  /**
   * The panel's one polite live region for processing cards. It speaks a card's OUTCOME
   * (complete, needs attention, transcript ready) once, never progress ticks or the move
   * into "Finishing up", and says nothing about the cards that were already present when
   * the panel opened.
   */
  export let jobs: ProcessingJobModel[];

  const ANNOUNCE_KEYS: Record<FinalJobState, string> = {
    complete: 'notifications.job.announceDone',
    attention: 'notifications.job.announceAttention',
    ready: 'notifications.job.announceReady',
  };

  let seen: Map<string, ProcessingJobModel['state']> | null = null;
  let message = '';

  $: announce(jobs);

  function announce(list: ProcessingJobModel[]) {
    const next = new Map(list.map((j) => [j.key, j.state] as const));
    if (seen !== null) {
      const lines: string[] = [];
      for (const j of list) {
        if (isFinalJobState(j.state) && seen.has(j.key) && seen.get(j.key) !== j.state) {
          lines.push($t(ANNOUNCE_KEYS[j.state], { filename: j.filename }));
        }
      }
      if (lines.length > 0) message = lines.join(' ');
    }
    seen = next;
  }
</script>

<div class="job-announcer" role="status" aria-live="polite" aria-atomic="true">{message}</div>

<style>
  .job-announcer {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
