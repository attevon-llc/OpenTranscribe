<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import {
    getRedactionPolicy,
    updateRedactionPolicy,
    triggerRedactionReindex,
    type RedactionPolicy,
  } from '$lib/api/redactionSettings';

  let loading = true;
  let saving = false;
  let reindexing = false;
  let policy: RedactionPolicy | null = null;
  let original: RedactionPolicy | null = null;

  $: hasChanges = policy && original && JSON.stringify(policy) !== JSON.stringify(original);
  $: settingsModalStore.setDirty('redaction-policy', !!hasChanges);

  onMount(() => {
    (async () => {
      try {
        const p = await getRedactionPolicy();
        policy = p;
        original = JSON.parse(JSON.stringify(p));
      } catch {
        toastStore.error($t('settings.redactionPolicy.loadError'));
      } finally {
        loading = false;
      }
    })();
  });

  async function save() {
    if (!policy) return;
    saving = true;
    try {
      const updated = await updateRedactionPolicy(policy);
      policy = updated;
      original = JSON.parse(JSON.stringify(updated));
      settingsModalStore.clearDirty('redaction-policy');
      toastStore.success($t('settings.redactionPolicy.saved'));
    } catch {
      toastStore.error($t('settings.redactionPolicy.saveError'));
    } finally {
      saving = false;
    }
  }

  async function reindex() {
    reindexing = true;
    try {
      await triggerRedactionReindex(true);
      toastStore.success($t('settings.redactionPolicy.reindexQueued'));
    } catch {
      toastStore.error($t('settings.redactionPolicy.saveError'));
    } finally {
      reindexing = false;
    }
  }
</script>

<div class="redaction-policy-settings">
  {#if loading || !policy}
    <div class="loading-state">{$t('common.loading')}</div>
  {:else}
    <div class="settings-form">
      <p class="intro-desc">{$t('settings.redactionPolicy.description')}</p>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.forceCategoriesTitle')}</h3>
        <p class="section-desc">{$t('settings.redactionPolicy.forceCategoriesDesc')}</p>

        <div class="setting-row">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={policy.force_pii} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.forcePii')}</span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forcePiiHelp')}</p>

        <div class="setting-row" style="margin-top: 0.75rem;">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={policy.force_toxicity} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.forceToxicity')}</span>
          </label>
        </div>
        {#if policy.force_toxicity}
          <div class="form-group" style="margin-top: 0.5rem;">
            <label class="form-label" for="force-tox">
              {$t('settings.redactionPolicy.forceToxicityThreshold')}
              <span class="value-badge">{policy.force_toxicity_threshold.toFixed(2)}</span>
            </label>
            <input
              id="force-tox"
              class="range-input"
              type="range"
              min="0"
              max="1"
              step="0.05"
              bind:value={policy.force_toxicity_threshold}
            />
          </div>
        {/if}

        <div class="setting-row" style="margin-top: 0.75rem;">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={policy.force_profanity} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.forceProfanity')}</span>
          </label>
        </div>
      </div>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.mandatesTitle')}</h3>

        <div class="setting-row">
          <label class="toggle-label">
            <input
              type="checkbox"
              class="toggle-input"
              bind:checked={policy.force_export_redacted}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.forceExportRedacted')}</span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forceExportRedactedHelp')}</p>

        <div class="setting-row" style="margin-top: 0.75rem;">
          <label class="toggle-label">
            <input
              type="checkbox"
              class="toggle-input"
              bind:checked={policy.force_redact_before_llm}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.forceRedactBeforeLlm')}</span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forceRedactBeforeLlmHelp')}</p>
      </div>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.enhancedTitle')}</h3>
        <div class="setting-row">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={policy.pii_use_gliner} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.redactionPolicy.piiUseGliner')}</span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.piiUseGlinerHelp')}</p>
      </div>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.maintenanceTitle')}</h3>
        <p class="section-desc">{$t('settings.redactionPolicy.reindexHelp')}</p>
        <button class="btn btn-secondary" on:click={reindex} disabled={reindexing}>
          {reindexing ? $t('common.saving') : $t('settings.redactionPolicy.reindex')}
        </button>
      </div>

      <div class="button-row">
        <button class="btn btn-primary" on:click={save} disabled={saving || !hasChanges}>
          {saving ? $t('common.saving') : $t('settings.redactionPolicy.save')}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .redaction-policy-settings {
    padding: 0.5rem 0;
  }
  .loading-state {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    color: var(--text-secondary);
  }
  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }
  .intro-desc {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin: 0;
    line-height: 1.5;
  }
  .settings-section {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.25rem;
  }
  .section-title {
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }
  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0 0 1rem 0;
  }
  .field-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0.375rem 0 0 0;
    line-height: 1.5;
  }
  .form-group {
    margin: 0;
  }
  .form-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }
  .value-badge {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.12);
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
  }
  .range-input {
    width: 100%;
    accent-color: var(--primary-color);
  }
  .setting-row {
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    user-select: none;
  }
  .toggle-input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }
  .toggle-switch {
    position: relative;
    width: 36px;
    height: 20px;
    background-color: var(--border-color);
    border-radius: 10px;
    transition: background-color 0.2s ease;
    flex-shrink: 0;
  }
  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.2s ease;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  }
  .toggle-input:checked + .toggle-switch {
    background-color: var(--primary-color);
  }
  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }
  .toggle-text {
    font-size: 0.875rem;
    color: var(--text-color);
  }
  .button-row {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
  }
</style>
