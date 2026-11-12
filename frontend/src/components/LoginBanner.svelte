<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import axiosInstance from '$lib/axios';
  import { colorsFor } from '$lib/utils/classificationColors';

  /**
   * The acknowledgment is being recorded server-side
   * (`POST /auth/banner/acknowledge`). Disables both actions so a double click
   * cannot fire two writes.
   */
  export let pending: boolean = false;
  /** Why the last acknowledgment attempt failed. Empty when there is nothing to say. */
  export let errorMessage: string = '';
  /**
   * The user already accepted a banner, but the wording has since changed and the
   * server expired that consent (`reason: "banner_text_changed"`). Saying so is
   * the difference between "the notice was updated" and "this is broken".
   */
  export let noticeUpdated: boolean = false;

  const dispatch = createEventDispatcher();

  let banner = {
    enabled: false,
    text: '',
    classification: 'UNCLASSIFIED',
    requires_acknowledgment: false
  };
  let loading = true;

  $: colors = colorsFor(banner.classification);


  onMount(async () => {
    try {
      const response = await axiosInstance.get('/auth/banner');
      banner = response.data;
    } catch (error) {
      console.error('Failed to fetch login banner:', error);
    } finally {
      loading = false;
    }
  });

  function handleAcknowledge() {
    dispatch('acknowledge');
  }

  // Declining is the parent's business: it ends the session and returns to a
  // clean login page. `window.location.href = 'about:blank'` used to live here,
  // and modern browsers commonly block it — leaving the user staring at the
  // banner with no feedback at all.
  function handleDecline() {
    dispatch('decline');
  }
</script>

{#if !loading && banner.enabled}
  <div class="banner-overlay">
    <div class="banner-modal">
      <div
        class="classification-header"
        style="background-color: {colors.bg}; color: {colors.text};"
      >
        {banner.classification}
      </div>

      <div class="banner-content">
        <h2>{$t('loginBanner.title')}</h2>

        {#if noticeUpdated}
          <p class="notice-updated" role="status">{$t('loginBanner.noticeUpdated')}</p>
        {/if}

        <div class="banner-text">
          {banner.text}
        </div>

        <p class="legal-notice">
          {$t('loginBanner.consentText')}
        </p>

        {#if errorMessage}
          <p class="banner-error" role="alert">{errorMessage}</p>
        {/if}
      </div>

      <div class="banner-actions">
        <button class="btn-decline" on:click={handleDecline} disabled={pending}>
          {$t('loginBanner.decline')}
        </button>
        <button class="btn-acknowledge" on:click={handleAcknowledge} disabled={pending}>
          {pending ? $t('loginBanner.acknowledging') : $t('loginBanner.acknowledge')}
        </button>
      </div>

      <div
        class="classification-footer"
        style="background-color: {colors.bg}; color: {colors.text};"
      >
        {banner.classification}
      </div>
    </div>
  </div>
{/if}

<style>
  .banner-overlay {
    position: fixed;
    inset: 0;
    background: var(--modal-backdrop);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: var(--z-critical);
  }

  .banner-modal {
    background: var(--surface-color);
    max-width: 800px;
    max-height: 90vh;
    overflow-y: auto;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-xl);
  }

  .classification-header,
  .classification-footer {
    padding: var(--space-2);
    text-align: center;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 2px;
  }

  .banner-content {
    padding: var(--space-6);
    color: var(--text-primary);
  }

  .banner-content h2 {
    margin: 0 0 var(--space-4);
    font-size: var(--font-size-2xl);
  }

  .banner-text {
    white-space: pre-wrap;
    font-size: var(--font-size-md);
    line-height: 1.6;
    margin: var(--space-4) 0;
    padding: var(--space-4);
    background: var(--surface-secondary);
    border-radius: var(--radius-sm);
  }

  .legal-notice {
    font-size: var(--font-size-md);
    color: var(--text-secondary);
    font-style: italic;
  }

  .notice-updated {
    margin: 0 0 var(--space-4);
    padding: var(--space-3) var(--space-4);
    border-inline-start: 4px solid var(--warning-text);
    border-radius: var(--radius-sm);
    background: var(--warning-bg);
    color: var(--warning-text);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
  }

  .banner-error {
    margin: var(--space-4) 0 0;
    padding: var(--space-3) var(--space-4);
    border-inline-start: 4px solid var(--error-text);
    border-radius: var(--radius-sm);
    background: var(--error-bg);
    color: var(--error-text);
    font-size: var(--font-size-md);
  }

  .banner-actions {
    display: flex;
    gap: var(--space-4);
    justify-content: center;
    padding: var(--space-4);
    border-top: 1px solid var(--border-color);
  }

  .btn-acknowledge {
    background: var(--primary-color);
    color: var(--text-on-solid);
    padding: var(--space-3) var(--space-8);
    border: none;
    border-radius: var(--radius-sm);
    font-weight: bold;
    cursor: pointer;
  }

  .btn-decline {
    background: var(--button-secondary-bg-hover);
    color: var(--text-secondary);
    padding: var(--space-3) var(--space-8);
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .btn-acknowledge:disabled,
  .btn-decline:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
