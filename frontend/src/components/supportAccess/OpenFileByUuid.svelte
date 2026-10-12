<script lang="ts">
  import { goto } from '$app/navigation';
  import { t } from '$stores/locale';
  import { isUuid } from '$lib/supportAccess/format';

  /**
   * A personal-workspace grant reaches one file at a time, by uuid (the support ticket
   * names it), never by browsing. The shape check is UX only; the server decides access.
   */
  export let idPrefix = 'open-file';

  let value = '';
  let invalid = false;

  function open() {
    const uuid = value.trim();
    if (!isUuid(uuid)) {
      invalid = true;
      return;
    }
    invalid = false;
    void goto(`/files/${uuid}`);
  }
</script>

<form class="open-file" on:submit|preventDefault={open}>
  <label for="{idPrefix}-input">{$t('supportAccess.openFile.label')}</label>
  <div class="row">
    <input
      id="{idPrefix}-input"
      type="text"
      autocomplete="off"
      placeholder={$t('supportAccess.openFile.placeholder')}
      aria-invalid={invalid}
      aria-describedby={invalid ? `${idPrefix}-error` : undefined}
      bind:value
      on:input={() => (invalid = false)}
    />
    <button type="submit" class="btn btn-primary">{$t('supportAccess.action.openFile')}</button>
  </div>
  {#if invalid}
    <p id="{idPrefix}-error" class="error" role="alert">{$t('supportAccess.openFile.invalid')}</p>
  {/if}
</form>

<style>
  .open-file {
    margin: 0.5rem 0 1rem 0;
  }
  label {
    display: block;
    margin-bottom: 0.25rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
  }
  .row {
    display: flex;
    gap: 0.5rem;
  }
  input {
    flex: 1;
    min-width: 0;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.875rem;
  }
  input:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .error {
    margin: 0.35rem 0 0 0;
    font-size: 0.75rem;
    color: var(--error-color);
  }
</style>
