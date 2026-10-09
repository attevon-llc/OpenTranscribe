<script context="module" lang="ts">
  export interface TargetChoice {
    uuid: string;
    label: string;
  }
</script>

<script lang="ts">
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';
  import SearchableSelect from '$components/ui/SearchableSelect.svelte';
  import { SupportAccessApi, type AccessLevel, type TargetKind } from '$lib/api/supportAccess';
  import { AdminApi } from '$lib/api/admin';
  import { durationLabel } from '$lib/supportAccess/format';

  /**
   * The fields a normal request and a break-glass request share. The parent owns the
   * values (all `bind:`) and decides what is submittable.
   */
  export let targetKind: TargetKind = 'organization';
  export let target: TargetChoice | null = null;
  export let level: AccessLevel = 'read';
  export let duration = 60;
  export let reason = '';
  export let durations: number[] = [15, 30, 60, 120, 240, 480];
  /** Distinguishes the radio groups when two instances could exist in one document. */
  export let idPrefix = 'grant';

  const REASON_MAX = 2000;
  const LEVELS: AccessLevel[] = ['read', 'write'];
  let query = '';

  function pickKind(kind: TargetKind) {
    if (kind === targetKind) return;
    targetKind = kind;
    target = null;
    query = '';
  }

  async function search(q: string): Promise<TargetChoice[]> {
    if (targetKind === 'organization') {
      const orgs = await SupportAccessApi.searchOrganizations(q, 10);
      return orgs.map((o) => ({ uuid: o.uuid, label: o.name }));
    }
    const { users } = await AdminApi.searchUsers({ query: q, limit: 10 });
    return users.map((u) => ({
      uuid: u.uuid,
      label: u.full_name ? `${u.full_name} (${u.email})` : u.email,
    }));
  }

  function choose(event: CustomEvent<TargetChoice>) {
    target = event.detail;
    query = '';
  }
</script>

<fieldset class="field">
  <legend>{$t('supportAccess.request.targetType')}</legend>
  <label class="radio">
    <input
      type="radio"
      name="{idPrefix}-kind"
      checked={targetKind === 'organization'}
      on:change={() => pickKind('organization')}
    />
    {$t('supportAccess.request.targetOrganization')}
  </label>
  <label class="radio">
    <input
      type="radio"
      name="{idPrefix}-kind"
      checked={targetKind === 'personal'}
      on:change={() => pickKind('personal')}
    />
    {$t('supportAccess.request.targetUser')}
  </label>
</fieldset>

<div class="field" role="group" aria-labelledby="{idPrefix}-target-label">
  {#if target}
    <div class="picked">
      <span id="{idPrefix}-target-label" class="picked-label">{target.label}</span>
      <button
        type="button"
        class="btn btn-secondary btn-row"
        on:click={() => (target = null)}
      >
        {$t('supportAccess.request.changeTarget')}
      </button>
    </div>
  {:else}
    <span id="{idPrefix}-target-label" class="label">
      {targetKind === 'organization'
        ? $t('supportAccess.request.searchOrganization')
        : $t('supportAccess.request.searchUser')}
    </span>
    {#key targetKind}
      <SearchableSelect
        bind:value={query}
        fetchFn={search}
        getLabel={(item) => item.label}
        placeholder={targetKind === 'organization'
          ? $t('supportAccess.request.searchOrganization')
          : $t('supportAccess.request.searchUser')}
        on:select={choose}
      />
    {/key}
  {/if}
</div>

<fieldset class="field">
  <legend>{$t('supportAccess.request.level')}</legend>
  {#each LEVELS as option (option)}
    <label class="radio">
      <input
        type="radio"
        name="{idPrefix}-level"
        value={option}
        checked={level === option}
        on:change={() => (level = option)}
      />
      {$t(SUPPORT_LEVEL_KEYS[option])}
    </label>
  {/each}
</fieldset>

<div class="field">
  <label for="{idPrefix}-duration" class="label">{$t('supportAccess.request.duration')}</label>
  <select id="{idPrefix}-duration" bind:value={duration}>
    {#each durations as minutes (minutes)}
      <option value={minutes}>{durationLabel(minutes, $t)}</option>
    {/each}
  </select>
</div>

<div class="field">
  <label for="{idPrefix}-reason" class="label">{$t('supportAccess.request.reason')}</label>
  <textarea
    id="{idPrefix}-reason"
    rows="4"
    maxlength={REASON_MAX}
    aria-describedby="{idPrefix}-reason-hint"
    bind:value={reason}
  ></textarea>
  <div id="{idPrefix}-reason-hint" class="hint">
    <span>{$t('supportAccess.request.reasonHint')}</span>
    <span class="count"><bdi>{$t('supportAccess.request.reasonCount', { count: reason.length })}</bdi></span>
  </div>
</div>

<style>
  .field {
    margin: 0 0 1rem 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  legend,
  .label {
    display: block;
    margin-bottom: 0.35rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
  }
  .radio {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-inline-end: 1.25rem;
    font-size: 0.875rem;
    color: var(--text-color);
    white-space: nowrap;
  }
  /* The app-wide `input { width: 100% }` would otherwise stretch the radio and wrap its label. */
  .radio input {
    width: auto;
    flex: none;
    margin: 0;
  }
  select,
  textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.875rem;
    font-family: inherit;
  }
  textarea {
    resize: vertical;
  }
  select:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .hint {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .picked {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-secondary);
  }
  .picked-label {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .btn-row {
    padding: 0.3rem 0.7rem;
    font-size: 0.75rem;
  }
</style>
