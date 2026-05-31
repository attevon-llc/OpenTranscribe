<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import {
    getRedactionSettings,
    updateRedactionSettings,
    resetRedactionSettings,
    getRedactionDefaults,
    DEFAULT_REDACTION_SETTINGS,
    type RedactionSettings,
    type RedactionSystemDefaults,
  } from '$lib/api/redactionSettings';

  // Inline Feather-style icons (no emojis in the UI).
  const lockSvg =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
  const clockSvg =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
  const globeSvg =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';

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
    (async () => {
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

<div class="content-redaction-settings">
  {#if loading}
    <div class="loading-state">{$t('common.loading')}</div>
  {:else}
    <div class="settings-form">
      <p class="intro-desc">{$t('settings.contentRedaction.description')}</p>

      <!-- General -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.general')}</h3>
        <div class="setting-row">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={settings.enabled} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.contentRedaction.enabled')}</span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.contentRedaction.enabledHelp')}</p>
        <p class="perf-note">
          <span class="perf-icon">{@html clockSvg}</span>
          {$t('settings.contentRedaction.performanceNote')}
        </p>

        <div class="form-group" style="margin-top: 1rem;">
          <label class="form-label" for="redaction-style">
            {$t('settings.contentRedaction.style')}
          </label>
          <select id="redaction-style" class="form-select" bind:value={settings.style}>
            {#each defaults?.available_styles ?? [] as style}
              <option value={style}>{$t(`settings.contentRedaction.styleOption.${style}`)}</option>
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
        </div>
      </div>

      <!-- Detectors -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.detectors')}</h3>
        <p class="section-desc">{$t('settings.contentRedaction.detectorsDesc')}</p>
        {#if defaults}
          <p class="lang-note">
            <span class="lang-icon">{@html globeSvg}</span>
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
          {#each defaults?.available_detectors ?? [] as det}
            <label class="chip" class:active={settings.detectors.includes(det)}>
              <input
                type="checkbox"
                checked={settings.detectors.includes(det)}
                on:change={() => (settings.detectors = toggleInList(settings.detectors, det))}
              />
              <span>{$t(`settings.contentRedaction.detector.${det}`)}</span>
            </label>
          {/each}
        </div>
      </div>

      <!-- Categories -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.categories')}</h3>
        <div class="chip-grid">
          {#each defaults?.available_categories ?? [] as cat}
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
              <span>{$t(`settings.contentRedaction.category.${cat}`)}</span>
              {#if locked}
                <span class="lock-badge" title={$t('settings.contentRedaction.requiredByAdmin')}>
                  {@html lockSvg}
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
          {#each defaults?.available_pii_entities ?? [] as ent}
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
      </div>

      <!-- Toxicity threshold -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.toxicity')}</h3>
        <div class="form-group">
          <label class="form-label" for="tox-threshold">
            {$t('settings.contentRedaction.toxicityThreshold')}
            <span class="value-badge">{settings.toxicity_threshold.toFixed(2)}</span>
          </label>
          <input
            id="tox-threshold"
            class="range-input"
            type="range"
            min="0"
            max="1"
            step="0.05"
            bind:value={settings.toxicity_threshold}
          />
          <p class="input-hint">{$t('settings.contentRedaction.toxicityThresholdHelp')}</p>
        </div>
      </div>

      <!-- Custom words & allowlist -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.wordLists')}</h3>
        <div class="form-group">
          <label class="form-label" for="custom-words">
            {$t('settings.contentRedaction.customWords')}
          </label>
          <textarea
            id="custom-words"
            class="form-textarea"
            rows="3"
            bind:value={customWordsText}
            placeholder={$t('settings.contentRedaction.customWordsPlaceholder')}
          ></textarea>
        </div>
        <div class="form-group">
          <label class="form-label" for="allowlist">
            {$t('settings.contentRedaction.allowlist')}
          </label>
          <textarea
            id="allowlist"
            class="form-textarea"
            rows="2"
            bind:value={allowlistText}
            placeholder={$t('settings.contentRedaction.allowlistPlaceholder')}
          ></textarea>
        </div>
      </div>

      <!-- Privacy -->
      <div class="settings-section">
        <h3 class="section-title">{$t('settings.contentRedaction.privacy')}</h3>
        <div class="setting-row" class:disabled-toggle={llmLocked}>
          <label class="toggle-label">
            <input
              type="checkbox"
              class="toggle-input"
              bind:checked={settings.redact_before_llm}
              disabled={llmLocked}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text">
              {$t('settings.contentRedaction.redactBeforeLlm')}
              {#if llmLocked}<span class="lock-badge">{@html lockSvg}</span>{/if}
            </span>
          </label>
        </div>
        <p class="field-desc">{$t('settings.contentRedaction.redactBeforeLlmHelp')}</p>

        <div class="setting-row" class:disabled-toggle={exportLocked} style="margin-top: 0.75rem;">
          <label class="toggle-label">
            <input
              type="checkbox"
              class="toggle-input"
              bind:checked={settings.default_export_redacted}
              disabled={exportLocked}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text">
              {$t('settings.contentRedaction.exportRedacted')}
              {#if exportLocked}<span class="lock-badge">{@html lockSvg}</span>{/if}
            </span>
          </label>
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
    margin-bottom: 0;
  }
  .form-group + .form-group {
    margin-top: 1rem;
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

  .form-select,
  .form-textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
    font-family: inherit;
  }
  .form-textarea {
    resize: vertical;
    line-height: 1.5;
  }
  .form-select:focus,
  .form-textarea:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(var(--primary-color-rgb), 0.1);
  }

  .input-hint {
    margin: 0.5rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .value-badge {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.12);
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
  }

  .perf-note {
    margin: 0.5rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-color);
    background: rgba(var(--warning-color-rgb, 234, 179, 8), 0.12);
    border-radius: 6px;
    padding: 0.5rem 0.65rem;
    line-height: 1.5;
  }

  .example-box {
    margin-top: 0.6rem;
    padding: 0.6rem 0.75rem;
    background: var(--background-color);
    border: 1px dashed var(--border-color);
    border-radius: 6px;
  }
  .example-label {
    display: block;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin-bottom: 0.25rem;
  }
  .example-text {
    font-size: 0.85rem;
    color: var(--text-color);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .ex-blur {
    filter: blur(5px);
    background: var(--surface-hover, rgba(127, 127, 127, 0.15));
    border-radius: 3px;
    user-select: none;
  }

  .range-input {
    width: 100%;
    accent-color: var(--primary-color);
  }

  /* Chip multiselect */
  .chip-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background: var(--background-color);
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.8125rem;
    user-select: none;
    transition:
      background-color 0.12s ease,
      border-color 0.12s ease,
      color 0.12s ease;
  }
  .chip.small {
    font-size: 0.75rem;
    padding: 0.3rem 0.6rem;
  }
  .chip:hover {
    border-color: var(--primary-color);
  }
  .chip.active {
    background: rgba(var(--primary-color-rgb), 0.12);
    border-color: var(--primary-color);
    color: var(--text-color);
    font-weight: 500;
  }
  .chip.locked {
    border-color: var(--warning-color);
    background: rgba(var(--warning-color-rgb, 234, 179, 8), 0.12);
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
    color: var(--warning-color);
  }
  .perf-note {
    display: flex;
    align-items: flex-start;
    gap: 0.4rem;
  }
  .perf-icon,
  .lang-icon {
    flex-shrink: 0;
    display: inline-flex;
    color: var(--text-muted);
  }
  .lang-icon {
    vertical-align: -2px;
    margin-right: 0.25rem;
  }

  .lang-note {
    font-size: 0.75rem;
    color: var(--text-muted);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.6rem 0.75rem;
    margin: 0 0 0.75rem 0;
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
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .disabled-toggle {
    opacity: 0.65;
  }

  .button-row {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
  }
</style>
