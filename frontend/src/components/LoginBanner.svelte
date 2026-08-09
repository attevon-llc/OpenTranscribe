<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { axiosInstance } from '$lib/axios';

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

  const classificationColors: Record<string, string> = {
    'UNCLASSIFIED': '#4ade80',
    'CUI': '#a855f7',
    'FOUO': '#3b82f6',
    'CONFIDENTIAL': '#3b82f6',
    'SECRET': '#ef4444',
    'TOP SECRET': '#f97316',
    'TOP SECRET//SCI': '#eab308'
  };

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
        style="background-color: {classificationColors[banner.classification] || '#4ade80'}"
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
        style="background-color: {classificationColors[banner.classification] || '#4ade80'}"
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
    background: rgba(0, 0, 0, 0.8);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
  }

  .banner-modal {
    background: white;
    max-width: 800px;
    max-height: 90vh;
    overflow-y: auto;
    border-radius: 8px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .classification-header,
  .classification-footer {
    padding: 8px;
    text-align: center;
    font-weight: bold;
    color: white;
    text-transform: uppercase;
    letter-spacing: 2px;
  }

  .banner-content {
    padding: 24px;
    color: #1f2937;
  }

  .banner-content h2 {
    margin: 0 0 16px;
    font-size: 1.5rem;
  }

  .banner-text {
    white-space: pre-wrap;
    font-size: 14px;
    line-height: 1.6;
    margin: 16px 0;
    padding: 16px;
    background: #f5f5f5;
    border-radius: 4px;
  }

  .legal-notice {
    font-size: 0.875rem;
    color: #6b7280;
    font-style: italic;
  }

  /* The card is deliberately light in both themes (a paper notice), so these
     two use fixed colours chosen for contrast on white rather than theme vars. */
  .notice-updated {
    margin: 0 0 16px;
    padding: 12px 16px;
    border-left: 4px solid #b45309;
    border-radius: 4px;
    background: #fef3c7;
    color: #7c2d12;
    font-size: 0.9rem;
    font-weight: 600;
  }

  .banner-error {
    margin: 16px 0 0;
    padding: 12px 16px;
    border-left: 4px solid #b91c1c;
    border-radius: 4px;
    background: #fee2e2;
    color: #7f1d1d;
    font-size: 0.875rem;
  }

  .banner-actions {
    display: flex;
    gap: 16px;
    justify-content: center;
    padding: 16px;
    border-top: 1px solid #eee;
  }

  .btn-acknowledge {
    background: var(--color-primary, #3b82f6);
    color: white;
    padding: 12px 32px;
    border: none;
    border-radius: 4px;
    font-weight: bold;
    cursor: pointer;
  }

  .btn-decline {
    background: #ddd;
    color: #666;
    padding: 12px 32px;
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }

  .btn-acknowledge:disabled,
  .btn-decline:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
