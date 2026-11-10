<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import RangeSlider from '$components/ui/RangeSlider.svelte';
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
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else}
    <div class="settings-form">
      <p class="intro-desc">{$t('settings.redactionPolicy.description')}</p>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.forceCategoriesTitle')}</h3>
        <p class="section-desc">{$t('settings.redactionPolicy.forceCategoriesDesc')}</p>

        <div class="setting-row">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-1" checked={policy.force_pii} onchange={(v) => { if (policy) policy.force_pii = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-1">{$t('settings.redactionPolicy.forcePii')}</label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forcePiiHelp')}</p>

        <div class="setting-row" style="margin-top: 0.75rem;">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-2" checked={policy.force_toxicity} onchange={(v) => { if (policy) policy.force_toxicity = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-2">{$t('settings.redactionPolicy.forceToxicity')}</label>
          </div>
        </div>
        {#if policy.force_toxicity}
          <div class="form-field">
            <RangeSlider
              id="force-tox"
              label={$t('settings.redactionPolicy.forceToxicityThreshold')}
              min={0}
              max={1}
              step={0.05}
              bind:value={policy.force_toxicity_threshold}
            />
          </div>
        {/if}

        <div class="setting-row" style="margin-top: 0.75rem;">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-3" checked={policy.force_profanity} onchange={(v) => { if (policy) policy.force_profanity = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-3">{$t('settings.redactionPolicy.forceProfanity')}</label>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.mandatesTitle')}</h3>

        <div class="setting-row">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-4" checked={policy.force_export_redacted} onchange={(v) => { if (policy) policy.force_export_redacted = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-4">{$t('settings.redactionPolicy.forceExportRedacted')}</label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forceExportRedactedHelp')}</p>

        <div class="setting-row" style="margin-top: 0.75rem;">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-5" checked={policy.force_redact_before_llm} onchange={(v) => { if (policy) policy.force_redact_before_llm = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-5">{$t('settings.redactionPolicy.forceRedactBeforeLlm')}</label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.redactionPolicy.forceRedactBeforeLlmHelp')}</p>
      </div>

      <div class="settings-section">
        <h3 class="section-title">{$t('settings.redactionPolicy.enhancedTitle')}</h3>
        <div class="setting-row">
          <div class="toggle-label">
            <Toggle id="redactionpolicysettings-toggle-6" checked={policy.pii_use_gliner} onchange={(v) => { if (policy) policy.pii_use_gliner = v; }} />
            <label class="toggle-text" for="redactionpolicysettings-toggle-6">{$t('settings.redactionPolicy.piiUseGliner')}</label>
          </div>
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
    padding: var(--space-2) 0;
  }
  .settings-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
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
    border-radius: var(--radius-lg);
    padding: var(--space-5);
  }
  .section-title {
    font-size: 0.95rem;
    font-weight: var(--font-weight-semibold);
    margin: 0 0 var(--space-1) 0;
    color: var(--text-color);
  }
  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0 0 var(--space-4) 0;
  }
  .field-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: var(--space-1-5) 0 0 0;
    line-height: 1.5;
  }
  .setting-row {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    user-select: none;
  }
  .toggle-text {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }
  .button-row {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-3);
  }
</style>
