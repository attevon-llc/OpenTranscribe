<script lang="ts">
  /**
   * The fields of one legal notice: type, date received, an opaque reference and the internal
   * basis. Shared by the quarantine modal and the notices panel so both record the same shape.
   * Deliberately no field for the sender's name or contact details: the record is built to
   * outlive everything, so those stay in the operator's own ticketing system.
   */
  import { t } from '$stores/locale';
  import { TAKEDOWN_NOTICE_TYPE_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import {
    BASIS_MAX_LENGTH,
    NOTICE_TYPES,
    REFERENCE_MAX_LENGTH,
    todayLocal,
    type NoticeDraft,
  } from '$lib/takedownBasis';

  export let draft: NoticeDraft;
  export let disabled = false;
  /** Prefix that keeps element ids unique when two instances exist on a page. */
  export let idPrefix = 'notice';

  const maxDate = todayLocal();
</script>

<div class="notice-fields">
  <label class="field" for={`${idPrefix}-type`}>
    {$t('takedown.fields.type')}
    <select id={`${idPrefix}-type`} bind:value={draft.noticeType} {disabled}>
      {#each NOTICE_TYPES as type (type)}
        <option value={type}>{labelFor($t, TAKEDOWN_NOTICE_TYPE_KEYS, type)}</option>
      {/each}
    </select>
  </label>

  <label class="field" for={`${idPrefix}-received`}>
    {$t('takedown.fields.receivedAt')}
    <input
      id={`${idPrefix}-received`}
      type="date"
      bind:value={draft.receivedDate}
      max={maxDate}
      {disabled}
    />
  </label>

  <label class="field" for={`${idPrefix}-reference`}>
    {$t('takedown.fields.reference')}
    <input
      id={`${idPrefix}-reference`}
      type="text"
      bind:value={draft.reference}
      maxlength={REFERENCE_MAX_LENGTH}
      {disabled}
    />
    <span class="help">{$t('takedown.fields.referenceHelp')}</span>
  </label>

  <label class="field" for={`${idPrefix}-basis`}>
    {$t('takedown.fields.basis')}
    <textarea
      id={`${idPrefix}-basis`}
      bind:value={draft.basis}
      maxlength={BASIS_MAX_LENGTH}
      rows="3"
      {disabled}
    ></textarea>
    <span class="help">{$t('takedown.fields.basisHelp')}</span>
  </label>
</div>

<style>
  .notice-fields {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
  }

  .field input,
  .field select,
  .field textarea {
    padding: 0.4rem 0.5rem;
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-regular);
  }

  .field textarea {
    resize: vertical;
    min-height: 60px;
  }

  .help {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
  }
</style>
