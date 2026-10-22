<script lang="ts">
  /** A takedown link's (or a derived notice's) status as a coloured chip, light and dark. */
  import { t } from '$stores/locale';
  import { TAKEDOWN_STATUS_KEYS } from '$lib/i18n/keyMaps';

  export let status: 'open' | 'counter_notice' | 'upheld' | 'released' | 'resolved';
</script>

<span class="chip chip-{status}">{$t(TAKEDOWN_STATUS_KEYS[status])}</span>

<style>
  .chip {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
  }

  /* Theme tokens switch with [data-theme], so one rule serves light and dark. */
  .chip-open {
    background: rgba(var(--warning-color-rgb), 0.15);
    color: var(--color-warning-text);
  }

  .chip-counter_notice {
    background: rgba(var(--primary-color-rgb), 0.14);
    color: var(--color-info-text);
  }

  .chip-upheld {
    background: rgba(var(--error-color-rgb), 0.14);
    color: var(--color-error-text);
  }

  .chip-released,
  .chip-resolved {
    background: rgba(var(--success-color-rgb), 0.14);
    color: var(--color-success-text);
  }
</style>
