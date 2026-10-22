<script lang="ts">
  import { t } from '$stores/locale';
  import type { ProcessingJobModel } from '$lib/notifications/processingJob';

  /**
   * The panel's one polite live region for processing cards. It speaks a card's OUTCOME
   * (finished or failed) once, never progress ticks, and says nothing about the cards that
   * were already present when the panel opened.
   */
  export let jobs: ProcessingJobModel[];

  let seen: Map<string, ProcessingJobModel['state']> | null = null;
  let message = '';

  $: announce(jobs);

  function announce(list: ProcessingJobModel[]) {
    const next = new Map(list.map((j) => [j.key, j.state] as const));
    if (seen !== null) {
      const changed = list.filter(
        (j) => j.state !== 'running' && seen?.has(j.key) && seen.get(j.key) !== j.state
      );
      if (changed.length > 0) {
        message = changed
          .map((j) =>
            $t(j.state === 'done' ? 'notifications.job.announceDone' : 'notifications.job.announceFailed', {
              filename: j.filename,
            })
          )
          .join(' ');
      }
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
