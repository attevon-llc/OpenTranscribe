<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import {
    getSpeakerAttributeSettings,
    updateSpeakerAttributeSettings,
    resetSpeakerAttributeSettings,
    type SpeakerAttributeSettings,
  } from '$lib/api/speakerAttributeSettings';
  import { refreshSpeakerAttributePrefs } from '$stores/speakerAttributePrefs';

  let settings: SpeakerAttributeSettings = {
    detection_enabled: true,
    gender_detection_enabled: true,
    show_attributes_on_cards: true,
  };

  let originalSettings: SpeakerAttributeSettings = { ...settings };
  let loading = true;
  let saving = false;
  let error = '';
  let successMessage = '';

  $: isDirty =
    settings.detection_enabled !== originalSettings.detection_enabled ||
    settings.gender_detection_enabled !== originalSettings.gender_detection_enabled ||
    settings.show_attributes_on_cards !== originalSettings.show_attributes_on_cards;

  const dispatch = createEventDispatcher<{ change: { hasChanges: boolean } }>();
  $: dispatch('change', { hasChanges: isDirty });

  onMount(loadSettings);

  async function loadSettings() {
    loading = true;
    error = '';
    try {
      settings = await getSpeakerAttributeSettings();
      originalSettings = { ...settings };
    } catch (e) {
      error = $t('settings.speakerAttributes.loadFailed');
      console.error(e);
    } finally {
      loading = false;
    }
  }

  async function saveSettings() {
    saving = true;
    error = '';
    successMessage = '';
    try {
      settings = await updateSpeakerAttributeSettings(settings);
      originalSettings = { ...settings };
      // Speaker cards read the cached preference; without this they keep the old badge
      // visibility until the cache goes stale.
      void refreshSpeakerAttributePrefs(true);
      successMessage = $t('settings.speakerAttributes.saved');
      setTimeout(() => (successMessage = ''), 3000);
    } catch (e) {
      error = $t('settings.speakerAttributes.saveFailed');
      console.error(e);
    } finally {
      saving = false;
    }
  }

  async function resetToDefaults() {
    saving = true;
    error = '';
    try {
      await resetSpeakerAttributeSettings();
      await loadSettings();
      void refreshSpeakerAttributePrefs(true);
      successMessage = $t('settings.speakerAttributes.reset');
      setTimeout(() => (successMessage = ''), 3000);
    } catch (e) {
      error = $t('settings.speakerAttributes.resetFailed');
      console.error(e);
    } finally {
      saving = false;
    }
  }
</script>

<div class="settings-section">
  <h3 class="section-title">{$t('settings.speakerAttributes.title')}</h3>
  <p class="section-description">{$t('settings.speakerAttributes.description')}</p>

  {#if loading}
    <div class="skeleton-rows">
      <div class="skeleton-row">
        <div class="skeleton-text wide"></div>
        <div class="skeleton-toggle"></div>
      </div>
      <div class="skeleton-row sub">
        <div class="skeleton-text"></div>
        <div class="skeleton-toggle"></div>
      </div>
      <div class="skeleton-row sub">
        <div class="skeleton-text wide"></div>
        <div class="skeleton-toggle"></div>
      </div>
    </div>
  {:else}
    <div class="settings-group">
      <div class="setting-row">
        <div class="setting-info">
          <label class="setting-label" for="detection-enabled">
            {$t('settings.speakerAttributes.enableDetection')}
          </label>
          <p class="setting-description">
            {$t('settings.speakerAttributes.enableDetectionDesc')}
          </p>
          <p class="setting-note" data-testid="llm-coupling-note">
            {$t('settings.speakerAttributes.llmCouplingNote')}
          </p>
        </div>
        <label class="toggle">
          <input
            type="checkbox"
            id="detection-enabled"
            bind:checked={settings.detection_enabled}
          />
          <span class="toggle-slider"></span>
        </label>
      </div>

      {#if settings.detection_enabled}
        <div class="setting-row sub-setting">
          <div class="setting-info">
            <label class="setting-label" for="gender-detection">
              {$t('settings.speakerAttributes.genderDetection')}
            </label>
            <p class="setting-description">
              {$t('settings.speakerAttributes.genderDetectionDesc')}
            </p>
          </div>
          <label class="toggle">
            <input
              type="checkbox"
              id="gender-detection"
              bind:checked={settings.gender_detection_enabled}
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <div class="setting-row sub-setting">
          <div class="setting-info">
            <label class="setting-label" for="show-on-cards">
              {$t('settings.speakerAttributes.showOnCards')}
            </label>
            <p class="setting-description">
              {$t('settings.speakerAttributes.showOnCardsDesc')}
            </p>
          </div>
          <label class="toggle">
            <input
              type="checkbox"
              id="show-on-cards"
              bind:checked={settings.show_attributes_on_cards}
            />
            <span class="toggle-slider"></span>
          </label>
        </div>
      {/if}
    </div>

    {#if error}
      <div class="error-message">{error}</div>
    {/if}

    {#if successMessage}
      <div class="success-message">{successMessage}</div>
    {/if}

    <div class="button-group">
      <button class="btn btn-secondary" on:click={resetToDefaults} disabled={saving}>
        {$t('settings.speakerAttributes.resetDefaults')}
      </button>
      <button
        class="btn btn-primary"
        on:click={saveSettings}
        disabled={saving || !isDirty}
      >
        {saving ? $t('common.saving') : $t('settings.speakerAttributes.save')}
      </button>
    </div>
  {/if}
</div>

<style>
  .settings-section {
    padding: 0;
  }

  .settings-section h3 {
    font-size: 1.1rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
    color: var(--text-color, #e0e0e0);
  }

  .section-description {
    font-size: 0.85rem;
    color: var(--text-secondary, #999);
    margin-bottom: 1.5rem;
    line-height: 1.4;
  }

  .settings-group {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    padding: 0.75rem;
    border-radius: 8px;
    background: var(--background-color, #2a2a2a);
    border: 1px solid var(--border-color);
  }

  .setting-row.sub-setting {
    margin-left: 1.5rem;
    background: var(--surface-color, #333);
  }

  .setting-info {
    flex: 1;
  }

  .setting-label {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color, #e0e0e0);
    display: block;
    margin-bottom: 0.25rem;
  }

  .setting-description {
    font-size: 0.8rem;
    color: var(--text-secondary, #999);
    margin: 0;
    line-height: 1.3;
  }

  .setting-note {
    font-size: 0.775rem;
    color: var(--text-muted, #999);
    margin: 0.375rem 0 0;
    line-height: 1.3;
  }

  .toggle {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
    margin-top: 0.1rem;
  }

  .toggle input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background-color: var(--border-color, #555);
    border-radius: 24px;
    transition: 0.2s;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    border-radius: 50%;
    transition: 0.2s;
  }

  .toggle input:checked + .toggle-slider {
    background-color: var(--primary-color, #4a9eff);
  }

  .toggle input:checked + .toggle-slider::before {
    transform: translateX(20px);
  }

  .button-group {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    margin-top: 1rem;
  }

  .button-group .btn-secondary {
    margin-right: auto;
  }

  .error-message {
    color: var(--error-color, #ff6b6b);
    font-size: 0.85rem;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: rgba(255, 107, 107, 0.1);
    border-radius: 6px;
  }

  .success-message {
    color: var(--success-color, #51cf66);
    font-size: 0.85rem;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: rgba(81, 207, 102, 0.1);
    border-radius: 6px;
  }

  .skeleton-rows {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .skeleton-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem;
    border-radius: 8px;
    background: var(--background-color, #2a2a2a);
    border: 1px solid var(--border-color);
  }

  .skeleton-row.sub {
    margin-left: 1.5rem;
    background: var(--surface-color, #333);
  }

  .skeleton-text {
    height: 14px;
    width: 140px;
    border-radius: 4px;
    background: var(--border-color, #444);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-text.wide {
    width: 200px;
  }

  .skeleton-toggle {
    height: 24px;
    width: 44px;
    border-radius: 12px;
    background: var(--border-color, #444);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  @media (max-width: 768px) {
    .setting-row.sub-setting {
      margin-left: 0;
    }

    .button-group {
      flex-wrap: wrap;
    }

    .button-group .btn-secondary {
      margin-right: 0;
    }

    .button-group .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
