<script lang="ts">
  /**
   * The signed-in user's certificate details, for PKI-authenticated accounts.
   *
   * Fetches its own data. It previously read `$userStore.certificate`, a field
   * nothing ever set — `/api/auth/me` does not serve certificate metadata, only
   * `/api/auth/me/certificate` does — so `hasCertificate` was permanently false
   * and this panel silently rendered the no-certificate state for every user,
   * including those with a valid certificate.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { getCertificateInfo, type CertificateInfo } from '$lib/api/certificate';

  let certificate: CertificateInfo | undefined;
  let loadFailed = false;

  onMount(async () => {
    try {
      certificate = await getCertificateInfo();
    } catch {
      // Non-fatal: the rest of the security panel is unaffected, so this
      // degrades to showing nothing rather than breaking the page.
      loadFailed = true;
    }
  });

  $: hasCertificate = certificate?.has_certificate || false;

  // The backend returns JSON null for an absent field, so this takes null as
  // well as undefined — the widened shape is the accurate one.
  function formatDate(dateString?: string | null): string {
    if (!dateString) return $t('common.notAvailable');
    return new Date(dateString).toLocaleString();
  }

  // Check if certificate expires soon (30 days)
  $: expiresInDays = certificate?.valid_until
    ? Math.floor((new Date(certificate.valid_until).getTime() - Date.now()) / (1000 * 60 * 60 * 24))
    : null;
  $: showExpirationWarning = expiresInDays !== null && expiresInDays < 30 && expiresInDays > 0;
  $: isExpired = expiresInDays !== null && expiresInDays <= 0;
</script>

{#if hasCertificate && certificate}
  <div class="certificate-info">
    <div class="cert-header">
      <div class="cert-badge">
        <svg class="icon-shield" viewBox="0 0 24 24" width="20" height="20">
          <path fill="currentColor" d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/>
        </svg>
        <span>{$t('settings.certificate.authenticatedWith')}</span>
      </div>
    </div>

    {#if isExpired}
      <div class="warning-banner expired">
        {$t('settings.certificate.expired')}
      </div>
    {:else if showExpirationWarning}
      <div class="warning-banner">
        {$t('settings.certificate.expiresIn', { days: expiresInDays }) || `Your certificate expires in ${expiresInDays} days`}
      </div>
    {/if}

    <div class="cert-grid">
      <div class="cert-item">
        <span class="cert-label">{$t('settings.certificate.organization')}</span>
        <span>{certificate.organization || $t('common.notAvailable')}</span>
      </div>

      <div class="cert-item">
        <span class="cert-label">{$t('settings.certificate.organizationalUnit')}</span>
        <span>{certificate.organizational_unit || $t('common.notAvailable')}</span>
      </div>

      <div class="cert-item">
        <span class="cert-label">{$t('settings.certificate.serialNumber')}</span>
        <span class="mono">{certificate.serial_number || $t('common.notAvailable')}</span>
      </div>

      <div class="cert-item">
        <span class="cert-label">{$t('settings.certificate.validFrom')}</span>
        <span>{formatDate(certificate.valid_from)}</span>
      </div>

      <div class="cert-item">
        <span class="cert-label">{$t('settings.certificate.validUntil')}</span>
        <span class:expired={isExpired}>{formatDate(certificate.valid_until)}</span>
      </div>

      <div class="cert-item full-width">
        <span class="cert-label">{$t('settings.certificate.subjectDN')}</span>
        <pre>{certificate.subject_dn || $t('common.notAvailable')}</pre>
      </div>

      <div class="cert-item full-width">
        <span class="cert-label">{$t('settings.certificate.issuerDN')}</span>
        <pre>{certificate.issuer_dn || $t('common.notAvailable')}</pre>
      </div>

      <div class="cert-item full-width">
        <span class="cert-label">{$t('settings.certificate.fingerprint')}</span>
        <span class="mono fingerprint">{certificate.fingerprint || $t('common.notAvailable')}</span>
      </div>
    </div>
  </div>
{/if}

<style>
  .certificate-info {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
    margin-top: 1rem;
  }

  .cert-header {
    margin-bottom: 1rem;
  }

  .cert-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--success-color, #22c55e);
    color: white;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    font-size: 0.875rem;
  }

  .warning-banner {
    background: rgba(234, 179, 8, 0.15);
    color: #ca8a04;
    padding: 0.75rem 1rem;
    border-radius: 6px;
    margin-bottom: 1rem;
    font-weight: 500;
    font-size: 0.8125rem;
    border: 1px solid rgba(234, 179, 8, 0.3);
  }

  .warning-banner.expired {
    background: rgba(239, 68, 68, 0.15);
    color: #dc2626;
    border-color: rgba(239, 68, 68, 0.3);
  }

  :global([data-theme='dark']) .warning-banner {
    background: rgba(234, 179, 8, 0.2);
    color: #fbbf24;
    border-color: rgba(234, 179, 8, 0.4);
  }

  :global([data-theme='dark']) .warning-banner.expired {
    background: rgba(239, 68, 68, 0.2);
    color: #f87171;
    border-color: rgba(239, 68, 68, 0.4);
  }

  .cert-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
  }

  .cert-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .cert-item.full-width {
    grid-column: span 2;
  }

  .cert-item .cert-label {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.05em;
  }

  .cert-item span,
  .cert-item pre {
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .cert-item pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;
    background: var(--surface-color);
    padding: 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    border: 1px solid var(--border-color);
    font-family: 'Courier New', Courier, monospace;
  }

  .mono {
    font-family: 'Courier New', Courier, monospace;
  }

  .fingerprint {
    font-size: 0.7rem;
    word-break: break-all;
  }

  .expired {
    color: #dc2626;
    font-weight: 600;
  }

  :global([data-theme='dark']) .expired {
    color: #f87171;
  }

  @media (max-width: 640px) {
    .cert-grid {
      grid-template-columns: 1fr;
    }
    .cert-item.full-width {
      grid-column: span 1;
    }
  }
</style>
