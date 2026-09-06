<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { isOnline } from '$stores/network';
  import { loadProtectedMediaAuthConfig, getAuthConfigForHost, type ProtectedMediaAuthConfig } from '$lib/services/configService';
  import { getDownloadSettings, getDownloadSystemDefaults, type DownloadSystemDefaults } from '$lib/api/downloadSettings';
  import Spinner from '$components/ui/Spinner.svelte';

  export let mediaUrl = '';
  export let processingUrl = false;

  const dispatch = createEventDispatcher<{
    urlChange: { url: string };
    cancel: void;
  }>();

  // Download quality options
  let downloadVideoQuality = '';
  let downloadAudioOnly = false;
  let downloadAudioQuality = '';
  let downloadInitialVideoQuality = '';
  let downloadInitialAudioOnly = false;
  let downloadInitialAudioQuality = '';
  let downloadDefaults: DownloadSystemDefaults | null = null;
  let showDownloadOptions = false;

  // Protected media auth
  let mediaUsername = '';
  let mediaPassword = '';
  let currentAuthConfig: ProtectedMediaAuthConfig | null = null;

  $: showProtectedMediaAuth = (() => {
    try {
      const url = new URL(mediaUrl);
      currentAuthConfig = getAuthConfigForHost(url.hostname);
      return currentAuthConfig?.auth_type === 'user_password';
    } catch {
      currentAuthConfig = null;
      return false;
    }
  })();

  export function getUrlPayloadExtras(): Record<string, any> {
    const extras: Record<string, any> = {};
    if (mediaUsername) extras.media_username = mediaUsername;
    if (mediaPassword) extras.media_password = mediaPassword;
    if (downloadVideoQuality !== downloadInitialVideoQuality) extras.video_quality = downloadVideoQuality;
    if (downloadAudioOnly !== downloadInitialAudioOnly) extras.audio_only = downloadAudioOnly;
    if (downloadAudioQuality !== downloadInitialAudioQuality) extras.audio_quality = downloadAudioQuality;
    return extras;
  }

  export function resetState() {
    mediaUrl = '';
    mediaUsername = '';
    mediaPassword = '';
    showDownloadOptions = false;
    downloadDefaults = null;
  }

  async function loadDownloadDefaults() {
    if (downloadDefaults) return;
    try {
      const [settings, defaults] = await Promise.all([
        getDownloadSettings(),
        getDownloadSystemDefaults()
      ]);
      downloadDefaults = defaults;
      downloadVideoQuality = settings.video_quality;
      downloadAudioOnly = settings.audio_only;
      downloadAudioQuality = settings.audio_quality;
      downloadInitialVideoQuality = settings.video_quality;
      downloadInitialAudioOnly = settings.audio_only;
      downloadInitialAudioQuality = settings.audio_quality;
    } catch (err) {
      console.error('Failed to load download defaults:', err);
    }
  }

  async function pasteFromClipboard() {
    try {
      if (!navigator.clipboard?.readText) {
        toastStore.info($t('uploader.useCtrlV'));
        return;
      }
      if (!window.isSecureContext) {
        toastStore.info($t('uploader.clipboardRequiresHttps'));
        return;
      }
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        mediaUrl = text.trim();
        dispatch('urlChange', { url: mediaUrl });
        toastStore.success($t('uploader.pastedFromClipboard'));
      } else {
        toastStore.info($t('uploader.clipboardEmpty'));
      }
    } catch (error: unknown) {
      if ((error as Error).name !== 'NotAllowedError') {
        toastStore.info($t('uploader.useCtrlV'));
      }
    }
  }

  function handleCancel() {
    if (processingUrl) {
      toastStore.info($t('uploader.urlProcessingCancelled'));
    }
    resetState();
    dispatch('cancel');
  }
</script>

<div class="url-panel">
  {#if !$isOnline}
    <div class="offline-banner">
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="15" y1="9" x2="9" y2="15"></line>
        <line x1="9" y1="9" x2="15" y2="15"></line>
      </svg>
      <div>
        <strong>{$t('uploader.noInternet')}</strong><br />
        {$t('uploader.mediaNeedsInternet')}
      </div>
    </div>
  {/if}

  <div class="url-input-section">
    <label for="media-url" class="url-label">
      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
      </svg>
      {$t('uploader.mediaUrl')}
    </label>
    <div class="url-input-wrapper">
      <input
        id="media-url"
        type="url"
        placeholder={$t('uploader.mediaUrlPlaceholder')}
        class="url-input"
        bind:value={mediaUrl}
        on:input={() => dispatch('urlChange', { url: mediaUrl })}
        disabled={processingUrl || !$isOnline}
      />
      <button
        type="button"
        class="paste-button"
        on:click={pasteFromClipboard}
        disabled={processingUrl || !$isOnline}
        title={$t('uploader.pasteTooltip')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect width="8" height="4" x="8" y="2" rx="1" ry="1"/>
          <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>
          <path d="M12 11h4"/>
          <path d="M12 16h4"/>
          <path d="M8 11h.01"/>
          <path d="M8 16h.01"/>
        </svg>
      </button>
    </div>

    {#if showProtectedMediaAuth}
      <div class="protected-media-auth">
        <div class="protected-media-header">{$t('uploader.protectedMediaCredentialsTitle')}</div>
        <div class="protected-media-fields">
          <div class="protected-media-field">
            <label for="media-username">{$t('uploader.protectedMediaUsernameLabel')}</label>
            <input id="media-username" type="text" class="protected-input" bind:value={mediaUsername} autocomplete="username" disabled={processingUrl || !$isOnline} />
          </div>
          <div class="protected-media-field">
            <label for="media-password">{$t('uploader.protectedMediaPasswordLabel')}</label>
            <input id="media-password" type="password" class="protected-input" bind:value={mediaPassword} autocomplete="current-password" disabled={processingUrl || !$isOnline} />
          </div>
        </div>
        <p class="protected-media-hint">{$t('uploader.protectedMediaHint')}</p>
      </div>
    {/if}

    <!-- Download Quality Options -->
    <details class="download-options" bind:open={showDownloadOptions} on:toggle={() => { if (showDownloadOptions && !downloadDefaults) loadDownloadDefaults(); }}>
      <summary class="download-options-summary">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        {$t('uploader.downloadOptions')}
      </summary>
      {#if downloadDefaults}
        <div class="download-options-content">
          <div class="download-option-row">
            <label for="dl-video-quality">{$t('settings.download.videoQuality')}</label>
            <select id="dl-video-quality" bind:value={downloadVideoQuality} disabled={downloadAudioOnly} class="download-select">
              {#each Object.entries(downloadDefaults.available_video_qualities) as [value, label]}
                <option {value}>{label}</option>
              {/each}
            </select>
          </div>
          <div class="download-option-row">
            <label for="dl-audio-only">{$t('settings.download.audioOnly')}</label>
            <label class="download-toggle">
              <input id="dl-audio-only" type="checkbox" bind:checked={downloadAudioOnly} />
              <span class="download-toggle-slider"></span>
            </label>
          </div>
          {#if downloadAudioOnly}
            <div class="download-option-row">
              <label for="dl-audio-quality">{$t('settings.download.audioQuality')}</label>
              <select id="dl-audio-quality" bind:value={downloadAudioQuality} class="download-select">
                {#each Object.entries(downloadDefaults.available_audio_qualities) as [value, label]}
                  <option {value}>{label}</option>
                {/each}
              </select>
            </div>
          {/if}
          <p class="download-options-hint">{$t('uploader.downloadOptionsHint')}</p>
        </div>
      {:else}
        <div class="download-options-loading">
          <Spinner size="small" />
          {$t('common.loading')}
        </div>
      {/if}
    </details>
  </div>

  <div class="url-info-footer">
    <p class="url-platforms-note">{$t('uploader.urlPlatformsNote')}</p>
    <p class="url-terms-notice">
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <path d="M12 16v-4"></path>
        <path d="M12 8h.01"></path>
      </svg>
      {$t('uploader.urlTermsNotice')}
    </p>
  </div>
</div>

<style>
  .url-panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .offline-banner {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.75rem;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: 8px;
    font-size: 0.8125rem;
    color: var(--text-primary);
  }

  .offline-banner svg { flex-shrink: 0; color: #ef4444; margin-top: 2px; }

  .url-input-section {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .url-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .url-label svg { color: #ff0000; }

  .url-input-wrapper {
    position: relative;
    display: flex;
    align-items: center;
  }

  .url-input {
    width: 100%;
    padding: 0.625rem 2.5rem 0.625rem 0.75rem;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 0.9375rem;
    background-color: var(--surface-color);
    color: var(--text-primary);
    transition: border-color 0.2s ease;
  }

  .url-input:focus { outline: none; border-color: var(--primary-color); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1); }
  .url-input:disabled { opacity: 0.6; cursor: not-allowed; }

  .paste-button {
    position: absolute;
    right: 8px;
    background: transparent;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    padding: 6px;
    border-radius: 4px;
    display: flex;
    align-items: center;
    transition: all 0.15s ease;
    z-index: 1;
  }

  .paste-button:hover:not(:disabled) { background: var(--button-hover); color: var(--primary-on-surface); }
  .paste-button:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Protected Media Auth */
  .protected-media-auth {
    border-radius: 8px;
    padding: 0.75rem;
    background-color: var(--surface-color);
    border: 1px dashed var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .protected-media-header { font-size: 0.8125rem; font-weight: 600; color: var(--text-primary); }

  .protected-media-fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.5rem 1rem;
  }

  .protected-media-field { display: flex; flex-direction: column; gap: 0.25rem; }
  .protected-media-field label { font-size: 0.75rem; color: var(--text-secondary); }

  .protected-input {
    width: 100%;
    padding: 0.375rem 0.5rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    color: var(--text-primary);
    font-size: 0.8125rem;
  }

  .protected-input:disabled { opacity: 0.6; cursor: not-allowed; }
  .protected-media-hint { font-size: 0.6875rem; color: var(--text-secondary); margin: 0; }

  /* Download Options */
  .download-options {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .download-options-summary {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-secondary);
    cursor: pointer;
    list-style: none;
    transition: background 0.15s ease;
  }

  .download-options-summary::-webkit-details-marker { display: none; }
  .download-options-summary:hover { background: var(--surface-color); }

  .download-options-content {
    padding: 0.75rem;
    border-top: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
  }

  .download-option-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .download-option-row label { font-size: 0.8125rem; color: var(--text-primary); }

  .download-select {
    padding: 0.3125rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 0.8125rem;
    min-width: 120px;
  }

  .download-select:disabled { opacity: 0.5; }

  .download-toggle { position: relative; display: inline-block; width: 36px; height: 20px; cursor: pointer; }
  .download-toggle input { opacity: 0; width: 0; height: 0; }

  .download-toggle-slider {
    position: absolute;
    inset: 0;
    background-color: var(--border-color);
    border-radius: 20px;
    transition: background 0.2s ease;
  }

  .download-toggle-slider::before {
    content: '';
    position: absolute;
    width: 16px;
    height: 16px;
    left: 2px;
    bottom: 2px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s ease;
  }

  .download-toggle input:checked + .download-toggle-slider { background-color: var(--primary-color, var(--primary-color)); }
  .download-toggle input:checked + .download-toggle-slider::before { transform: translateX(16px); }

  .download-options-hint { font-size: 0.6875rem; color: var(--text-secondary); margin: 0; }
  .download-options-loading { display: flex; align-items: center; gap: 0.5rem; padding: 0.75rem; font-size: 0.8125rem; color: var(--text-secondary); }

  /* URL Info Footer */
  .url-info-footer {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    padding: 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
  }

  .url-platforms-note { margin: 0; font-size: 0.6875rem; line-height: 1.5; color: var(--text-secondary); text-align: center; }

  .url-terms-notice {
    display: flex;
    align-items: baseline;
    gap: 0.375rem;
    margin: 0;
    font-size: 0.625rem;
    line-height: 1.4;
    color: var(--text-tertiary, var(--text-secondary));
  }

  .url-terms-notice svg { flex-shrink: 0; position: relative; top: 1px; }
</style>
