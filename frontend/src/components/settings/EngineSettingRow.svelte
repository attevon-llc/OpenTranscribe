<!--
  EngineSettingRow.svelte — one control of the Speaker Engine tab: a label, the control (slot),
  an optional one-line hint, and the override chrome.

  The "where does this value come from" badge and its Reset button only appear when the value is
  overridden (database or environment). A value left at the server default shows no badge: a
  "Default" chip on every row was noise, and its absence says the same thing.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';

  export let label: string;
  /** Id of the control the label belongs to; omit for a toggle that carries its own label. */
  export let labelFor: string | undefined = undefined;
  export let source: 'db' | 'env' | 'default' = 'default';
  export let resetting = false;
  export let disabled = false;
  /** One short line under the control. */
  export let hint = '';

  $: overridden = source !== 'default';
  $: badge = source === 'db' ? $t('settings.engineSettings.sourceDb') : $t('settings.engineSettings.sourceEnv');
</script>

<div class="form-row">
  <div class="form-field">
    <div class="field-label-row">
      {#if labelFor}
        <label for={labelFor}>{label}</label>
      {:else}
        <span class="field-name">{label}</span>
      {/if}
      {#if overridden}
        <span class="source-badge" class:source-db={source === 'db'} class:source-env={source === 'env'}>
          {badge}
        </span>
        <button
          type="button"
          class="reset-btn"
          on:click
          disabled={resetting || disabled}
          title={$t('settings.engineSettings.resetKey')}
        >
          {#if resetting}
            <Spinner size="small" />
          {:else}
            {$t('settings.engineSettings.resetKey')}
          {/if}
        </button>
      {/if}
    </div>
    <slot />
    {#if hint}
      <p class="field-hint">{hint}</p>
    {/if}
    <slot name="after" />
  </div>
</div>

<style>
  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
  }

  .field-label-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .field-label-row label,
  .field-label-row .field-name {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-color);
    flex: 1;
    min-width: 0;
  }

  .source-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.675rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .source-db {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
  }
  :global([data-theme='dark']) .source-db {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  .source-env {
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
  }
  :global([data-theme='dark']) .source-env {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .reset-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
    cursor: pointer;
    background: var(--surface-color);
    color: var(--text-muted);
    border: 1px solid var(--border-color);
    transition: all 0.15s;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .reset-btn:hover:not(:disabled) {
    background: var(--button-hover);
    color: var(--text-color);
  }

  .reset-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .field-hint {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  @media (max-width: 768px) {
    .field-label-row {
      gap: 0.375rem;
    }

    .form-field {
      padding: 0.75rem;
    }
  }
</style>
