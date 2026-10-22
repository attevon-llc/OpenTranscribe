<script lang="ts">
  /**
   * One notice with every file it covers (issue #1254): the internal basis, and per file the
   * reason the owner was shown, the status and the record-keeping actions. Recording a
   * counter-notice or an upheld decision never changes the file; only Release does.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { TakedownNoticesApi, type TakedownNotice } from '$lib/api/takedownNotices';
  import TakedownStatusChip from './TakedownStatusChip.svelte';

  export let notice: TakedownNotice;

  const dispatch = createEventDispatcher<{ changed: TakedownNotice }>();

  let busy = '';

  async function record(fileUuid: string, status: 'counter_notice' | 'upheld') {
    busy = `${fileUuid}:${status}`;
    try {
      const updated = await TakedownNoticesApi.recordOutcome(notice.uuid, fileUuid, status);
      toastStore.success($t('takedown.admin.outcomeRecorded'));
      dispatch('changed', updated);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('takedown.admin.outcomeFailed')));
    } finally {
      busy = '';
    }
  }

  /** `released` tells the owners of the released files that access is back; nobody else. */
  async function notify(event: 'quarantined' | 'released' = 'quarantined') {
    busy = 'notify';
    try {
      const result = await TakedownNoticesApi.notify(notice.uuid, event);
      if (result.sent > 0) {
        toastStore.success($t('takedown.admin.notified', { count: result.sent }));
      } else if (result.skipped_no_transport > 0) {
        toastStore.warning($t('takedown.admin.notifyNoTransport'));
      } else if (result.failed > 0) {
        toastStore.error($t('takedown.admin.notifyFailed'));
      } else {
        toastStore.info($t('takedown.admin.alreadyNotified'));
      }
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('takedown.admin.notifyFailed')));
    } finally {
      busy = '';
    }
  }

  // Only a file still under takedown can take an outcome; the server enforces the same rule.
  const inForce = (status: string) => status !== 'released';
</script>

<div class="detail">
  <p class="basis-line">
    <strong>{$t('takedown.fields.basis')}:</strong>
    {notice.basis}
  </p>

  {#if notice.links.length === 0}
    <p class="muted">{$t('takedown.admin.noFiles')}</p>
  {:else}
    <table class="links">
      <thead>
        <tr>
          <th>{$t('takedown.admin.columnFile')}</th>
          <th>{$t('takedown.admin.columnOwnerReason')}</th>
          <th>{$t('takedown.admin.columnStatus')}</th>
          <th>{$t('takedown.admin.columnActions')}</th>
        </tr>
      </thead>
      <tbody>
        {#each notice.links as link (link.uuid)}
          <tr>
            <td>
              {#if link.file_exists}
                {link.file_title}
              {:else}
                <span class="muted">{$t('takedown.admin.filePurged')}</span>
              {/if}
              <div class="uuid">{link.file_uuid}</div>
            </td>
            <td class="reason">{link.owner_reason}</td>
            <td><TakedownStatusChip status={link.status} /></td>
            <td>
              <div class="actions">
                {#if inForce(link.status) && link.file_exists}
                  {#if link.status !== 'counter_notice'}
                    <button
                      class="action-link"
                      disabled={busy !== ''}
                      on:click={() => record(link.file_uuid, 'counter_notice')}
                    >
                      {$t('takedown.admin.recordCounterNotice')}
                    </button>
                  {/if}
                  {#if link.status !== 'upheld'}
                    <button
                      class="action-link"
                      disabled={busy !== ''}
                      on:click={() => record(link.file_uuid, 'upheld')}
                    >
                      {$t('takedown.admin.markUpheld')}
                    </button>
                  {/if}
                {/if}
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="notify-row">
      {#if notice.links.some((l) => inForce(l.status))}
        <button class="btn btn-secondary" disabled={busy !== ''} on:click={() => notify('quarantined')}>
          {$t('takedown.admin.notifyOwners')}
        </button>
      {/if}
      {#if notice.links.some((l) => l.status === 'released')}
        <button class="btn btn-secondary" disabled={busy !== ''} on:click={() => notify('released')}>
          {$t('takedown.admin.notifyRestored')}
        </button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .detail {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.75rem;
    background: var(--bg-secondary);
    border-radius: 6px;
  }

  .basis-line,
  .muted {
    margin: 0;
    font-size: var(--font-size-sm);
  }

  .muted,
  .uuid {
    color: var(--text-secondary);
  }

  .uuid {
    font-size: var(--font-size-2xs);
    font-family: monospace;
  }

  .links {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .links th,
  .links td {
    padding: 0.4rem;
    border-bottom: 1px solid var(--border-color);
    text-align: start;
    vertical-align: top;
  }

  .reason {
    max-width: 260px;
    overflow-wrap: anywhere;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }

  .action-link {
    border: none;
    background: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    padding: 0;
    font-size: var(--font-size-sm);
  }

  .notify-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .action-link:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
