<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import Toggle from '$components/ui/Toggle.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { REDACTION_CATEGORY_KEYS, REDACTION_DETECTOR_KEYS, REDACTION_STYLE_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import RangeSlider from '$components/ui/RangeSlider.svelte';
  import {
    getRedactionSettings,
    updateRedactionSettings,
    resetRedactionSettings,
    getRedactionDefaults,
    DEFAULT_REDACTION_SETTINGS,
    type RedactionSettings,
    type RedactionSystemDefaults,
  } from '$lib/api/redactionSettings';

  let loading = true;
  let saving = false;
  let settings: RedactionSettings = { ...DEFAULT_REDACTION_SETTINGS };
  let original: RedactionSettings = { ...DEFAULT_REDACTION_SETTINGS };
  let defaults: RedactionSystemDefaults | null = null;

  let customWordsText = '';
  let allowlistText = '';

  $: lockedCategories = new Set(defaults?.locked_categories ?? []);
  $: exportLocked = defaults?.export_locked ?? false;
  $: llmLocked = defaults?.redact_before_llm_locked ?? false;

  $: hasChanges =
    JSON.stringify(settings) !== JSON.stringify(original) ||
    customWordsText !== original.custom_words.join('\n') ||
    allowlistText !== original.allowlist.join('\n');

  $: settingsModalStore.setDirty('content-redaction', hasChanges);

  onMount(() => {
    void (async () => {
      try {
        const [s, d] = await Promise.all([getRedactionSettings(), getRedactionDefaults()]);
        settings = s;
        original = JSON.parse(JSON.stringify(s));
        defaults = d;
        customWordsText = s.custom_words.join('\n');
        allowlistText = s.allowlist.join('\n');
      } catch {
        toastStore.error($t('settings.contentRedaction.loadError'));
      } finally {
        loading = false;
      }
    })();
  });

  function toggleInList(list: string[], value: string): string[] {
    return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  }

  // Live example so users see EXACTLY what they'll get for the chosen mask style.
  // Sample with one name, one phone, and one profane word.
  const _exParts = [
    { pre: "Hi, I'm ", word: 'John Smith', etype: 'NAME', cat: 'pii' },
    { pre: ', call ', word: '555-123-4567', etype: 'PHONE', cat: 'pii' },
    { pre: ' — this is ', word: 'bullshit', etype: 'PROFANITY', cat: 'profanity' },
    { pre: '.', word: '', etype: '', cat: '' },
  ];
  function maskWord(word: string, etype: string, style: string): string {
    if (!word) return '';
    if (style === 'asterisks') return '*'.repeat(word.length);
    if (style === 'first_letter') return word[0] + '*'.repeat(Math.max(0, word.length - 1));
    if (style === 'blur') return word; // rendered blurred via CSS in the preview
    return `[${etype}]`;
  }
  $: examplePlain = _exParts
    .map((p) => p.pre + (p.word ? maskWord(p.word, p.etype, settings.style) : ''))
    .join('');

  function toggleCategory(cat: string) {
    if (lockedCategories.has(cat)) return;
    settings.categories = toggleInList(settings.categories, cat);
  }

  async function save() {
    saving = true;
    try {
      settings.custom_words = customWordsText.split('\n').map((w) => w.trim()).filter(Boolean);
      settings.allowlist = allowlistText.split('\n').map((w) => w.trim()).filter(Boolean);
      const updated = await updateRedactionSettings(settings);
      settings = updated;
      original = JSON.parse(JSON.stringify(updated));
      customWordsText = updated.custom_words.join('\n');
      allowlistText = updated.allowlist.join('\n');
      settingsModalStore.clearDirty('content-redaction');
      toastStore.success($t('settings.contentRedaction.saved'));
    } catch {
      toastStore.error($t('settings.contentRedaction.saveError'));
    } finally {
      saving = false;
    }
  }

  async function reset() {
    saving = true;
    try {
      await resetRedactionSettings();
      const s = await getRedactionSettings();
      settings = s;
      original = JSON.parse(JSON.stringify(s));
      customWordsText = s.custom_words.join('\n');
      allowlistText = s.allowlist.join('\n');
      settingsModalStore.clearDirty('content-redaction');
      toastStore.success($t('settings.contentRedaction.resetDone'));
    } catch {
      toastStore.error($t('settings.contentRedaction.saveError'));
    } finally {
      saving = false;
    }
  }
</script>

<div class="content-redaction-settings form-panel">
  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else}
    <div class="settings-form">
      <p class="intro-desc">{$t('settings.contentRedaction.description')}</p>

      <!-- General -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.general')}</h3>
        <div class="setting-row">
          <div class="toggle-label">
            <Toggle checked={settings.enabled} onchange={(next) => (settings.enabled = next)} id="redaction-toggle-label-1" />
            <label class="toggle-text" for="redaction-toggle-label-1">{$t('settings.contentRedaction.enabled')}</label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.contentRedaction.enabledHelp')}</p>
        <p class="perf-note">
          <span class="perf-icon"><MenuIcon name="clock" size={14} /></span>
          {$t('settings.contentRedaction.performanceNote')}
        </p>

        <div class="form-stack">
        <FormField id="redaction-style" label={$t('settings.contentRedaction.style')} labelSize="sm">
          <select id="redaction-style" class="form-select" bind:value={settings.style}>
            {#each defaults?.available_styles ?? [] as style (style)}
              <option value={style}>{labelFor($t, REDACTION_STYLE_KEYS, style)}</option>
            {/each}
          </select>
          <!-- Live example of exactly what this style produces -->
          <div class="example-box">
            <span class="example-label">{$t('settings.contentRedaction.exampleLabel')}</span>
            {#if settings.style === 'blur'}
              <span class="example-text">
                Hi, I'm <span class="ex-blur">John Smith</span>, call
                <span class="ex-blur">555-123-4567</span> — this is
                <span class="ex-blur">bullshit</span>.
              </span>
            {:else}
              <span class="example-text">{examplePlain}</span>
            {/if}
          </div>
        </FormField>
        </div>
      </div>

      <!-- Detectors -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.detectors')}</h3>
        <p class="section-desc">{$t('settings.contentRedaction.detectorsDesc')}</p>
        {#if defaults}
          <p class="lang-note">
            <span class="lang-icon"><MenuIcon name="globe" size={14} /></span>
            {$t('settings.contentRedaction.languageSupport')}
            <br />
            <strong>{$t('settings.contentRedaction.detector.profanity')} / {$t('settings.contentRedaction.detector.pii')}:</strong>
            {defaults.pii_languages.join(', ')}
            &nbsp;·&nbsp;
            <strong>{$t('settings.contentRedaction.detector.toxicity')}:</strong>
            {defaults.toxicity_languages.join(', ')}
            <br />
            <span class="lang-note-hint">{$t('settings.contentRedaction.languageSkipNote')}</span>
          </p>
        {/if}
        <div class="chip-grid">
          {#each defaults?.available_detectors ?? [] as det (det)}
            <label class="chip" class:active={settings.detectors.includes(det)}>
              <input
                type="checkbox"
                checked={settings.detectors.includes(det)}
                on:change={() => (settings.detectors = toggleInList(settings.detectors, det))}
              />
              <span>{labelFor($t, REDACTION_DETECTOR_KEYS, det)}</span>
            </label>
          {/each}
        </div>
      </div>

      <!-- Categories -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.categories')}</h3>
        <div class="chip-grid">
          {#each defaults?.available_categories ?? [] as cat (cat)}
            {@const locked = lockedCategories.has(cat)}
            <label
              class="chip"
              class:active={settings.categories.includes(cat) || locked}
              class:locked
            >
              <input
                type="checkbox"
                checked={settings.categories.includes(cat) || locked}
                disabled={locked}
                on:change={() => toggleCategory(cat)}
              />
              <span>{labelFor($t, REDACTION_CATEGORY_KEYS, cat)}</span>
              {#if locked}
                <span class="lock-badge" title={$t('settings.contentRedaction.requiredByAdmin')}>
                  <MenuIcon name="lock" size={13} />
                </span>
              {/if}
            </label>
          {/each}
        </div>
      </div>

      <!-- PII entity types -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.piiEntities')}</h3>
        <div class="chip-grid">
          {#each defaults?.available_pii_entities ?? [] as ent (ent)}
            <label class="chip small" class:active={settings.pii_entities.includes(ent)}>
              <input
                type="checkbox"
                checked={settings.pii_entities.includes(ent)}
                on:change={() => (settings.pii_entities = toggleInList(settings.pii_entities, ent))}
              />
              <span>{ent}</span>
            </label>
          {/each}
        </div>
        <!-- Name detection is not exhaustive and users must be told, not left to
             discover it (issue #499). Measured on the shipped en_core_web_sm model:
             a surname can be tagged ORGANIZATION, and some names are missed entirely. -->
        <p class="perf-note">
          <span class="perf-icon"><MenuIcon name="warning" size={14} /></span>
          {$t('settings.contentRedaction.piiEntitiesWarning')}
        </p>
      </div>

      <!-- Toxicity threshold -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.toxicity')}</h3>
        <div class="form-field">
          <RangeSlider
            id="tox-threshold"
            label={$t('settings.contentRedaction.toxicityThreshold')}
            min={0}
            max={1}
            step={0.05}
            hint={$t('settings.contentRedaction.toxicityThresholdHelp')}
            bind:value={settings.toxicity_threshold}
          />
        </div>
      </div>

      <!-- Custom words & allowlist -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.wordLists')}</h3>
        <div class="form-stack">
        <FormField id="custom-words" label={$t('settings.contentRedaction.customWords')} labelSize="sm">
          <textarea
            id="custom-words"
            class="form-textarea"
            rows="3"
            bind:value={customWordsText}
            placeholder={$t('settings.contentRedaction.customWordsPlaceholder')}
          ></textarea>
        </FormField>
        <FormField id="allowlist" label={$t('settings.contentRedaction.allowlist')} labelSize="sm">
          <textarea
            id="allowlist"
            class="form-textarea"
            rows="2"
            bind:value={allowlistText}
            placeholder={$t('settings.contentRedaction.allowlistPlaceholder')}
          ></textarea>
        </FormField>
        </div>
      </div>

      <!-- Privacy -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.privacy')}</h3>
        <div class="setting-row" class:disabled-toggle={llmLocked}>
          <div class="toggle-label">
            <Toggle checked={settings.redact_before_llm} onchange={(next) => (settings.redact_before_llm = next)} disabled={llmLocked} id="redaction-toggle-label-2" />
            <label class="toggle-text" for="redaction-toggle-label-2">
              {$t('settings.contentRedaction.redactBeforeLlm')}
              {#if llmLocked}<span class="lock-badge"><MenuIcon name="lock" size={13} /></span>{/if}
            </label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.contentRedaction.redactBeforeLlmHelp')}</p>

        <div class="setting-row" class:disabled-toggle={exportLocked} style="margin-top: var(--space-3);">
          <div class="toggle-label">
            <Toggle checked={settings.default_export_redacted} onchange={(next) => (settings.default_export_redacted = next)} disabled={exportLocked} id="redaction-toggle-label-3" />
            <label class="toggle-text" for="redaction-toggle-label-3">
              {$t('settings.contentRedaction.exportRedacted')}
              {#if exportLocked}<span class="lock-badge"><MenuIcon name="lock" size={13} /></span>{/if}
            </label>
          </div>
        </div>
        <p class="field-desc">{$t('settings.contentRedaction.exportRedactedHelp')}</p>
      </div>

      <div class="button-row">
        <button class="btn btn-secondary" on:click={reset} disabled={saving}>
          {$t('settings.contentRedaction.reset')}
        </button>
        <button class="btn btn-primary" on:click={save} disabled={saving || !hasChanges}>
          {saving ? $t('common.saving') : $t('settings.contentRedaction.save')}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .content-redaction-settings {
    padding: var(--space-2) 0;
  }

  .intro-desc {
    font-size: var(--font-size-md);
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }

  .section-title {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    margin: 0 0 var(--space-1) 0;
    color: var(--text-color);
  }

  .section-desc {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0 0 var(--space-4) 0;
  }

  .form-select,
  .form-textarea {
    width: 100%;
  }
  .form-textarea {
    resize: vertical;
  }

  .perf-note {
    display: flex;
    align-items: flex-start;
    gap: var(--space-1-5);
    margin: var(--space-2) 0 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-color);
    background: rgba(var(--warning-color-rgb), 0.12);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-2-5);
    line-height: 1.5;
  }

  .example-box {
    margin-top: var(--space-2-5);
    padding: var(--space-2-5) var(--space-3);
    background: var(--background-color);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-md);
  }
  .example-label {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-bottom: var(--space-1);
  }
  .example-text {
    font-size: var(--font-size-md);
    color: var(--text-color);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .ex-blur {
    filter: blur(5px);
    background: var(--surface-hover);
    border-radius: var(--radius-sm);
    user-select: none;
  }

  /* Chip multiselect */
  .chip-grid {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-1-5) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-pill);
    background: var(--background-color);
    color: var(--text-secondary);
    cursor: pointer;
    font-size: var(--font-size-sm);
    user-select: none;
    transition:
      background-color var(--duration-fast) ease,
      border-color var(--duration-fast) ease,
      color var(--duration-fast) ease;
  }
  .chip.small {
    font-size: var(--font-size-xs);
    padding: var(--space-1) var(--space-2-5);
  }
  .chip:hover {
    border-color: var(--primary-color);
  }
  .chip.active {
    background: rgba(var(--primary-color-rgb), 0.12);
    border-color: var(--primary-color);
    color: var(--text-color);
    font-weight: var(--font-weight-medium);
  }
  .chip.locked {
    border-color: var(--warning-color);
    background: rgba(var(--warning-color-rgb), 0.12);
    cursor: not-allowed;
  }
  /* Hide the raw checkbox; the chip itself is the control. */
  .chip input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }
  .lock-badge {
    display: inline-flex;
    align-items: center;
    color: var(--warning-text);
  }
  .perf-icon,
  .lang-icon {
    flex-shrink: 0;
    display: inline-flex;
    color: var(--text-secondary);
  }
  .lang-icon {
    vertical-align: -2px;
    margin-inline-end: var(--space-1);
  }

  .lang-note {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--space-2-5) var(--space-3);
    margin: 0 0 var(--space-3) 0;
    line-height: 1.6;
  }
  .lang-note strong {
    color: var(--text-color);
  }
  .lang-note-hint {
    font-style: italic;
  }

  /* Toggle switch (matches Transcription settings) */
  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    user-select: none;
  }
  .toggle-text {
    cursor: pointer;
    font-size: var(--font-size-md);
    color: var(--text-color);
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }
  .disabled-toggle {
    opacity: 0.65;
  }
</style>
