<script lang="ts">
  /**
   * Government Classification Banner Component
   *
   * Displays a standard DoD/FedRAMP classification banner with proper styling
   * per MIL-STD-129 and DoDM 5200.01 marking requirements.
   *
   * Classification levels and their colors:
   * - UNCLASSIFIED: Green (#007a33)
   * - CUI/FOUO: Yellow/Gold (#ffc72c) with black text
   * - CONFIDENTIAL: Blue (#00538b)
   * - SECRET: Red (#c8102e)
   * - TOP SECRET: Orange (#ff6600)
   * - TOP SECRET/SCI: Yellow (#ffc72c) with black text
   */

  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let classification: 'UNCLASSIFIED' | 'CUI' | 'FOUO' | 'CONFIDENTIAL' | 'SECRET' | 'TOP SECRET' | 'TOP SECRET//SCI' = 'UNCLASSIFIED';
  export let bannerText: string = '';
  export let requireAcknowledgment: boolean = false;
  export let position: 'top' | 'both' = 'top';
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

  // Classification color mapping per DoD standards
  const classificationColors: Record<string, { bg: string; text: string }> = {
    'UNCLASSIFIED': { bg: '#007a33', text: '#ffffff' },
    'CUI': { bg: '#502b85', text: '#ffffff' },  // Purple for CUI per NARA standards
    'FOUO': { bg: '#ffc72c', text: '#000000' },
    'CONFIDENTIAL': { bg: '#0033a0', text: '#ffffff' },
    'SECRET': { bg: '#c8102e', text: '#ffffff' },
    'TOP SECRET': { bg: '#ff671f', text: '#000000' },
    'TOP SECRET//SCI': { bg: '#fce300', text: '#000000' },
  };

  // Default consent banner text (DoD standard)
  const defaultBannerText = `You are accessing a U.S. Government (USG) Information System (IS) that is provided for USG-authorized use only.

By using this IS (which includes any device attached to this IS), you consent to the following conditions:

- The USG routinely intercepts and monitors communications on this IS for purposes including, but not limited to, penetration testing, COMSEC monitoring, network operations and defense, personnel misconduct (PM), law enforcement (LE), and counterintelligence (CI) investigations.

- At any time, the USG may inspect and seize data stored on this IS.

- Communications using, or data stored on, this IS are not private, are subject to routine monitoring, interception, and search, and may be disclosed or used for any USG-authorized purpose.

- This IS includes security measures (e.g., authentication and access controls) to protect USG interests--not for your personal benefit or privacy.

- Notwithstanding the above, using this IS does not constitute consent to PM, LE or CI investigative searching or monitoring of the content of privileged communications, or work product, related to personal representation or services by attorneys, psychotherapists, or clergy, and their assistants. Such communications and work product are private and confidential. See User Agreement for details.`;

  $: colors = classificationColors[classification] || classificationColors['UNCLASSIFIED'];
  $: displayText = bannerText || defaultBannerText;

  function handleAcknowledge() {
    dispatch('acknowledge');
  }

  function handleDecline() {
    dispatch('decline');
  }
</script>

<!-- Top Banner -->
<div
  class="classification-banner"
  style="background-color: {colors.bg}; color: {colors.text};"
  role="banner"
  aria-label="Classification marking: {classification}"
>
  <span class="classification-text">{classification}</span>
</div>

{#if requireAcknowledgment}
  <!-- Full Consent Modal -->
  <div class="consent-overlay" role="dialog" aria-modal="true" aria-labelledby="consent-title">
    <div class="consent-modal">
      <div
        class="consent-header"
        style="background-color: {colors.bg}; color: {colors.text};"
      >
        <h2 id="consent-title">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
          U.S. Government Information System
        </h2>
        <span class="classification-badge">{classification}</span>
      </div>

      <div class="consent-body">
        <div class="warning-icon" aria-hidden="true">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
            <line x1="12" y1="9" x2="12" y2="13"/>
            <line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
        </div>

        {#if noticeUpdated}
          <p class="notice-updated" role="status">{$t('loginBanner.noticeUpdated')}</p>
        {/if}

        <div class="consent-text">
          {#each displayText.split('\n\n') as paragraph}
            <p>{paragraph}</p>
          {/each}
        </div>

        {#if errorMessage}
          <p class="consent-error" role="alert">{errorMessage}</p>
        {/if}
      </div>

      <div class="consent-footer">
        <button class="btn-decline" on:click={handleDecline} disabled={pending}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
          Exit System
        </button>
        <button class="btn-acknowledge" on:click={handleAcknowledge} disabled={pending}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
          {pending ? $t('loginBanner.acknowledging') : 'I Acknowledge & Consent'}
        </button>
      </div>
    </div>
  </div>
{/if}

{#if position === 'both'}
  <!-- Bottom Banner (sticky) -->
  <div
    class="classification-banner classification-banner-bottom"
    style="background-color: {colors.bg}; color: {colors.text};"
    role="contentinfo"
    aria-label="Classification marking: {classification}"
  >
    <span class="classification-text">{classification}</span>
  </div>
{/if}

<style>
  .classification-banner {
    width: 100%;
    text-align: center;
    padding: 4px 8px;
    font-family: 'Roboto Condensed', 'Arial Narrow', Arial, sans-serif;
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    /* Same tier as ToastContainer (H5) — both are globally-rendered and can be
       bottom-anchored (`.classification-banner-bottom`) at once; there is no
       measured reason to prefer one above the other, so they share a layer
       rather than guessing an order. Flagged for human visual confirmation. */
    z-index: var(--z-toast);
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
  }

  .classification-banner-bottom {
    top: auto;
    bottom: 0;
  }

  .classification-text {
    display: inline-block;
  }

  /* Consent Overlay */
  .consent-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: rgba(0, 0, 0, 0.85);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
    padding: 1rem;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .consent-modal {
    background-color: #ffffff;
    border-radius: 8px;
    max-width: 700px;
    width: 100%;
    max-height: 90vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  }

  .consent-header {
    padding: 1rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .consent-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .consent-header svg {
    flex-shrink: 0;
  }

  .classification-badge {
    padding: 0.25rem 0.75rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.5px;
    background-color: rgba(255, 255, 255, 0.2);
    white-space: nowrap;
  }

  .consent-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    background-color: #f8fafc;
  }

  .warning-icon {
    text-align: center;
    margin-bottom: 1rem;
    color: #dc2626;
  }

  .notice-updated {
    margin: 0 0 1rem;
    padding: 0.75rem 1rem;
    border-left: 4px solid #b45309;
    border-radius: 4px;
    background-color: #fef3c7;
    color: #7c2d12;
    font-size: 0.9rem;
    font-weight: 600;
  }

  .consent-error {
    margin: 1rem 0 0;
    padding: 0.75rem 1rem;
    border-left: 4px solid #b91c1c;
    border-radius: 4px;
    background-color: #fee2e2;
    color: #7f1d1d;
    font-size: 0.875rem;
  }

  .consent-text {
    color: #1f2937;
    font-size: 0.9rem;
    line-height: 1.6;
  }

  .consent-text p {
    margin-bottom: 1rem;
  }

  .consent-text p:last-child {
    margin-bottom: 0;
  }

  .consent-footer {
    padding: 1rem 1.5rem;
    background-color: #f1f5f9;
    display: flex;
    justify-content: flex-end;
    gap: 1rem;
    border-top: 1px solid #e2e8f0;
  }

  .btn-decline,
  .btn-acknowledge {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1.5rem;
    border-radius: 6px;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
  }

  .btn-decline {
    background-color: #6b7280;
    color: white;
  }

  .btn-decline:hover {
    background-color: #4b5563;
  }

  .btn-acknowledge {
    background-color: #059669;
    color: white;
  }

  .btn-acknowledge:hover {
    background-color: #047857;
  }

  .btn-decline:disabled,
  .btn-acknowledge:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Dark mode adjustments */
  :global(.dark) .consent-modal {
    background-color: #1e293b;
  }

  :global(.dark) .consent-body {
    background-color: #0f172a;
  }

  :global(.dark) .consent-text {
    color: #e2e8f0;
  }

  :global(.dark) .consent-footer {
    background-color: #1e293b;
    border-top-color: #334155;
  }

  /* Responsive */
  @media (max-width: 640px) {
    .consent-modal {
      margin: 0.5rem;
      max-height: 95vh;
    }

    .consent-header {
      flex-direction: column;
      align-items: flex-start;
    }

    .consent-footer {
      flex-direction: column;
    }

    .btn-decline,
    .btn-acknowledge {
      width: 100%;
      justify-content: center;
    }
  }
</style>
