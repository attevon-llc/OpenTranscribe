<script lang="ts">
  import { resolve } from '$app/paths';
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
    void goto(resolve('/files/[id]', { id: uuid }));
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
    margin: var(--space-2) 0 var(--space-4) 0;
  }
  label {
    display: block;
    margin-bottom: var(--space-1);
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
  }
  .row {
    display: flex;
    gap: var(--space-2);
  }
  input {
    flex: 1;
    min-width: 0;
  }
  .error {
    margin: 0.35rem 0 0 0;
    font-size: 0.75rem;
    color: var(--error-color);
  }
</style>
