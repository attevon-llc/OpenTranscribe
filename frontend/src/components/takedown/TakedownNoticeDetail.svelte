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

  async function notify() {
    busy = 'notify';
    try {
      const result = await TakedownNoticesApi.notify(notice.uuid);
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
            <td class="actions">
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
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <button class="btn" disabled={busy !== ''} on:click={notify}>
      {$t('takedown.admin.notifyOwners')}
    </button>
  {/if}
</div>

<style>
  .detail {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.75rem;
    background: var(--bg-secondary, rgba(0, 0, 0, 0.03));
    border-radius: 6px;
  }

  .basis-line,
  .muted {
    margin: 0;
    font-size: 0.8125rem;
  }

  .muted,
  .uuid {
    color: var(--text-secondary, var(--text-color));
  }

  .uuid {
    font-size: 0.6875rem;
    font-family: monospace;
  }

  .links {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .links th,
  .links td {
    padding: 0.4rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
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
    color: var(--accent-color, #2563eb);
    cursor: pointer;
    padding: 0;
    font-size: 0.8125rem;
  }

  .btn {
    align-self: flex-start;
    padding: 0.375rem 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.8125rem;
  }

  .action-link:disabled,
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
