<script lang="ts">
  /**
   * Placeholder shown in the transcript column while redaction detection is
   * still running and the transcript is therefore withheld.
   */
  import { t } from '$stores/locale';
</script>

<div class="redaction-pending">
  <div class="redaction-pending-chip">
    <span class="chip-dot"></span>
    {$t('settings.contentRedaction.processingChip')}
  </div>
  <p class="redaction-pending-msg">{$t('settings.contentRedaction.processingMessage')}</p>
</div>

<style>
  .redaction-pending {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 3rem 1rem;
    text-align: center;
  }
  .redaction-pending-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.9rem;
    border-radius: 999px;
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
    font-size: 0.85rem;
    font-weight: 600;
  }
  .redaction-pending-chip .chip-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--primary-color);
    animation: redaction-pulse 1.2s ease-in-out infinite;
  }
  @keyframes redaction-pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
  .redaction-pending-msg {
    color: var(--text-secondary);
    font-size: 0.85rem;
    max-width: 360px;
  }
</style>
