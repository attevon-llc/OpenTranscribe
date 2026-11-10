<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { recordingStore, recordingManager, isRecording, recordingDuration, audioLevel } from '$stores/recording';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { formatClock } from '$lib/utils/formatting';

  export let recordingSupported = true;
  export let maxRecordingDuration = 7200; // seconds
  export let recordingQuality = 'high';
  export let autoStopEnabled = true;

  // Browsers expose navigator.mediaDevices only in a secure context (HTTPS or localhost),
  // so on a plain-HTTP LAN address "not supported" is the address, not the browser.
  const insecureContext = typeof window !== 'undefined' && window.isSecureContext === false;

  const dispatch = createEventDispatcher<{
    recordingReady: void;
    clear: void;
  }>();

  $: recordedBlob = $recordingStore.recordedBlob;
  $: audioDevices = $recordingStore.audioDevices;
  $: selectedDeviceId = $recordingStore.selectedDeviceId;
  $: isPaused = $recordingStore.isPaused;

  async function startRecording() {
    try {
      await recordingManager.startRecording();
    } catch (err) {
      console.error('Recording error:', err);
    }
  }

  function stopRecording() {
    recordingManager.stopRecording();
    // After stopping, recording is ready
    dispatch('recordingReady');
  }

  function togglePause() {
    if (isPaused) {
      recordingManager.resumeRecording();
    } else {
      recordingManager.pauseRecording();
    }
  }

  function handleDeviceChange(event: Event) {
    const target = event.target as HTMLSelectElement;
    recordingStore.update(state => ({ ...state, selectedDeviceId: target.value }));
  }

  function clearRecording() {
    recordingManager.clearRecording();
    dispatch('clear');
  }
</script>

<div class="record-panel">
  {#if !recordingSupported}
    <div class="unsupported-msg" data-testid="recording-unsupported">
      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="12" y1="8" x2="12" y2="12"></line>
        <line x1="12" y1="16" x2="12.01" y2="16"></line>
      </svg>
      <span>{$t(insecureContext ? 'uploader.recordingRequiresHttps' : 'uploader.recordingNotSupported')}</span>
    </div>
  {:else}
    {#if audioDevices.length > 0}
      <div class="device-selector">
        <label for="audio-device-select" class="device-label">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a4 4 0 0 0-4 4v7a4 4 0 0 0 8 0V5a4 4 0 0 0-4-4z"></path>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
            <line x1="12" y1="19" x2="12" y2="23"></line>
            <line x1="8" y1="23" x2="16" y2="23"></line>
          </svg>
          {$t('uploader.microphoneDevice')}
        </label>
        <select
          id="audio-device-select"
          class="device-select"
          value={selectedDeviceId}
          on:change={handleDeviceChange}
          disabled={$isRecording}
          title={$t('uploader.selectMicrophoneTooltip')}
        >
          {#each audioDevices as device, i (i)}
            <option value={device.deviceId}>
              {device.label || `${$t('uploader.microphone')} ${i + 1}`}
            </option>
          {/each}
        </select>
      </div>
    {/if}

    <div class="recording-controls">
      {#if !$isRecording && !recordedBlob}
        <!-- Idle: Start button -->
        <button
          class="start-button"
          on:click={startRecording}
          title={$t('uploader.startRecordingTooltip', { max: Math.floor(maxRecordingDuration / 60) })}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a4 4 0 0 0-4 4v7a4 4 0 0 0 8 0V5a4 4 0 0 0-4-4z"></path>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
            <line x1="12" y1="19" x2="12" y2="23"></line>
            <line x1="8" y1="23" x2="16" y2="23"></line>
          </svg>
          {$t('uploader.startRecording')}
        </button>

      {:else if $isRecording}
        <!-- Active recording -->
        <div class="recording-active">
          <div class="status-row">
            <div class="recording-dot" class:paused={isPaused}></div>
            <span class="status-text">{isPaused ? $t('uploader.paused') : $t('uploader.recording')}</span>
            <span class="duration-badge">{formatClock($recordingDuration)}</span>
          </div>

          <div class="visualizer">
            <div class="level-meter">
              {#each Array(20) as _, i (i)}
                <div
                  class="level-bar"
                  class:active={($audioLevel / 100) * 20 > i}
                  class:low={i < 12}
                  class:medium={i >= 12 && i < 16}
                  class:high={i >= 16}
                ></div>
              {/each}
            </div>
          </div>

          <div class="control-row">
            <button class="ctrl-btn" on:click={togglePause} title={isPaused ? $t('uploader.resumeRecording') : $t('uploader.pauseRecording')}>
              {#if isPaused}
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
              {:else}
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>
              {/if}
            </button>
            <button class="ctrl-btn stop" on:click={stopRecording} title={$t('uploader.stopRecording')}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect></svg>
            </button>
          </div>
        </div>

      {:else if recordedBlob}
        <!-- Recording complete -->
        <div class="recording-complete">
          <div class="complete-info">
            <span class="complete-title">{$t('uploader.recordingComplete')}</span>
            <span class="complete-meta">{formatClock($recordingDuration)} &bull; {(recordedBlob.size / 1024 / 1024).toFixed(1)} MB</span>
          </div>
          <button class="ctrl-btn clear" on:click={clearRecording} title={$t('uploader.clearRecordingTooltip')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
            {$t('uploader.startOver')}
          </button>
        </div>
      {/if}
    </div>

    <div class="settings-summary">
      <span>{$t('uploader.recordingSettingsSummary', { max: Math.floor(maxRecordingDuration / 60), quality: recordingQuality, autoStop: autoStopEnabled ? $t('uploader.on') : $t('uploader.off') })}</span>
      <button type="button" class="settings-link" on:click={() => settingsModalStore.open('recording')} title={$t('uploader.changeSettingsTooltip')}>
        {$t('uploader.changeSettings')}
      </button>
    </div>
  {/if}
</div>

<style>
  .record-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .unsupported-msg {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-4);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: var(--radius-lg);
    font-size: var(--font-size-md);
    color: var(--text-primary);
  }

  .unsupported-msg svg { flex-shrink: 0; color: #ef4444; }

  /* Device selector */
  .device-selector {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
  }

  .device-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .device-select {
    width: 100%;
  }
  .device-select:disabled { opacity: 0.6; cursor: not-allowed; }

  /* Recording controls */
  .recording-controls {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-6) var(--space-4);
    border: 2px solid var(--border-color);
    border-radius: var(--radius-2xl);
    background-color: var(--surface-color);
    min-height: 150px;
    justify-content: center;
  }

  .start-button {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-6);
    border-radius: var(--radius-lg);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    cursor: pointer;
    border: 2px solid var(--error-solid);
    background-color: var(--error-solid);
    color: var(--text-on-solid);
    box-shadow: 0 2px 4px rgba(220, 38, 38, 0.2);
    transition: all var(--duration-base) ease;
  }

  .start-button:hover {
    background-color: #b91c1c;
    border-color: #b91c1c;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(220, 38, 38, 0.25);
  }

  /* Active recording */
  .recording-active {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    width: 100%;
    align-items: center;
  }

  .status-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    justify-content: center;
  }

  .recording-dot {
    width: 10px;
    height: 10px;
    border-radius: var(--radius-round);
    background-color: var(--error-solid);
    animation: pulse 1.5s ease-in-out infinite;
  }

  .recording-dot.paused {
    background-color: #f59e0b;
    animation: none;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .status-text { font-weight: var(--font-weight-semibold); font-size: var(--font-size-md); color: var(--text-primary); }

  .duration-badge {
    font-family: Monaco, Menlo, monospace;
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
    background: rgba(var(--accent-rgb), 0.1);
    padding: 0.1875rem var(--space-2-5);
    border-radius: var(--radius-sm);
  }

  .visualizer { width: 100%; display: flex; justify-content: center; }

  .level-meter {
    display: flex;
    gap: 3px;
    align-items: center;
    height: 30px;
    width: 50%;
    justify-content: center;
  }

  .level-bar {
    width: 10px;
    height: 22px;
    border-radius: 3px;
    background-color: var(--border-color);
    opacity: 0.3;
    transition: all 0.1s ease-out;
  }

  .level-bar.active.low { background-color: #10b981; opacity: 1; }
  .level-bar.active.medium { background-color: #f59e0b; opacity: 1; }
  .level-bar.active.high { background-color: var(--error-solid); opacity: 1; }

  .control-row { display: flex; gap: var(--space-3); justify-content: center; }

  .ctrl-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-1);
    padding: 0.4375rem var(--space-2-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-color);
    cursor: pointer;
    font-size: var(--font-size-xs);
    transition: all var(--duration-fast) ease;
  }

  .ctrl-btn:hover { background: var(--button-hover); }
  .ctrl-btn.stop:hover { background: rgba(239, 68, 68, 0.1); color: #dc2626; border-color: rgba(239, 68, 68, 0.3); }

  /* Recording complete */
  .recording-complete {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
  }

  .complete-info { text-align: center; }
  .complete-title { display: block; font-weight: var(--font-weight-semibold); font-size: var(--font-size-body); color: var(--text-primary); }
  .complete-meta { font-size: var(--font-size-sm); color: var(--text-secondary); }

  .ctrl-btn.clear:hover { background: rgba(107, 114, 128, 0.1); }

  /* Settings summary */
  .settings-summary {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--font-size-2xs);
    color: var(--text-tertiary);
    text-align: center;
  }

  .settings-link {
    background: none;
    border: none;
    color: var(--primary-on-surface);
    font-size: var(--font-size-2xs);
    cursor: pointer;
    text-decoration: underline;
    text-underline-offset: 2px;
    padding: 0;
  }

  .settings-link:hover { color: var(--primary-hover); }

  @media (prefers-reduced-motion: reduce) {
    .recording-dot { animation: none; }
  }
</style>
