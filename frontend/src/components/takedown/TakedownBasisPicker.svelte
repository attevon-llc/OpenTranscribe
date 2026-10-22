<script lang="ts">
  /**
   * How a takedown justifies itself (issue #1254): an existing recorded notice, a new notice
   * recorded inline, or an explicit "no notice (operator action)" with a stated reason. The
   * server refuses a takedown with neither, so this choice is required rather than defaulted.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { TakedownNoticesApi, type TakedownNotice } from '$lib/api/takedownNotices';
  import { BASIS_MAX_LENGTH, type TakedownBasis } from '$lib/takedownBasis';
  import TakedownNoticeFields from './TakedownNoticeFields.svelte';
  import { TAKEDOWN_NOTICE_TYPE_KEYS, labelFor } from '$lib/i18n/keyMaps';

  export let basis: TakedownBasis;
  export let disabled = false;

  const OPEN_NOTICES_LIMIT = 100;

  let openNotices: TakedownNotice[] = [];

  onMount(async () => {
    try {
      openNotices = (
        await TakedownNoticesApi.list({ status: 'open', limit: OPEN_NOTICES_LIMIT })
      ).notices.filter((n) => n.notice_type !== 'operator_action');
    } catch {
      // The other two choices still work; an empty select says there is nothing to pick.
      openNotices = [];
    }
    // With nothing to pick, the first useful choice is to record one.
    if (openNotices.length === 0 && basis.mode === 'existing') basis.mode = 'new';
  });

  function label(n: TakedownNotice): string {
    const date = new Date(n.received_at).toLocaleDateString();
    return `${labelFor($t, TAKEDOWN_NOTICE_TYPE_KEYS, n.notice_type)} · ${n.requester_reference ?? ''} · ${date}`;
  }
</script>

<fieldset class="basis" {disabled}>
  <legend>{$t('gallery.quarantine.basisLabel')}</legend>

  <label class="choice">
    <input type="radio" value="existing" bind:group={basis.mode} />
    {$t('gallery.quarantine.basisExisting')}
  </label>
  {#if basis.mode === 'existing'}
    <select
      class="indented"
      aria-label={$t('gallery.quarantine.noticeSelectLabel')}
      bind:value={basis.noticeUuid}
    >
      <option value="">
        {openNotices.length
          ? $t('gallery.quarantine.noticeSelectPlaceholder')
          : $t('gallery.quarantine.noticeSelectEmpty')}
      </option>
      {#each openNotices as n (n.uuid)}
        <option value={n.uuid}>{label(n)}</option>
      {/each}
    </select>
  {/if}

  <label class="choice">
    <input type="radio" value="new" bind:group={basis.mode} />
    {$t('gallery.quarantine.basisNew')}
  </label>
  {#if basis.mode === 'new'}
    <div class="indented">
      <TakedownNoticeFields bind:draft={basis.draft} idPrefix="quarantine-notice" {disabled} />
    </div>
  {/if}

  <label class="choice">
    <input type="radio" value="operator" bind:group={basis.mode} />
    {$t('gallery.quarantine.basisOperator')}
  </label>
  {#if basis.mode === 'operator'}
    <label class="indented field" for="quarantine-operator-reason">
      {$t('gallery.quarantine.operatorReasonLabel')}
      <textarea
        id="quarantine-operator-reason"
        bind:value={basis.operatorReason}
        maxlength={BASIS_MAX_LENGTH}
        rows="2"
        placeholder={$t('gallery.quarantine.operatorReasonPlaceholder')}
      ></textarea>
    </label>
  {/if}
</fieldset>

<style>
  .basis {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin: 0;
  }

  legend {
    padding: 0 0.25rem;
    font-weight: 600;
    font-size: 0.875rem;
  }

  .choice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
  }

  .indented {
    margin-left: 1.5rem;
    min-width: 0;
  }

  select.indented {
    box-sizing: border-box;
    width: calc(100% - 1.5rem);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-weight: 600;
    font-size: 0.8125rem;
  }

  select,
  textarea {
    padding: 0.4rem 0.5rem;
    font-size: 0.8125rem;
    font-weight: normal;
  }

  textarea {
    resize: vertical;
  }
</style>
