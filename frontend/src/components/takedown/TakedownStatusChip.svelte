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
    font-size: 0.6875rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .chip-open {
    background: rgba(217, 119, 6, 0.15);
    color: #b45309;
  }

  .chip-counter_notice {
    background: rgba(37, 99, 235, 0.12);
    color: #1d4ed8;
  }

  .chip-upheld {
    background: rgba(220, 38, 38, 0.12);
    color: #b91c1c;
  }

  .chip-released,
  .chip-resolved {
    background: rgba(22, 163, 74, 0.14);
    color: #15803d;
  }

  :global([data-theme='dark']) .chip-open {
    background: rgba(217, 119, 6, 0.25);
    color: #fbbf24;
  }

  :global([data-theme='dark']) .chip-counter_notice {
    background: rgba(96, 165, 250, 0.22);
    color: #93c5fd;
  }

  :global([data-theme='dark']) .chip-upheld {
    background: rgba(248, 113, 113, 0.22);
    color: #fca5a5;
  }

  :global([data-theme='dark']) .chip-released,
  :global([data-theme='dark']) .chip-resolved {
    background: rgba(74, 222, 128, 0.2);
    color: #86efac;
  }
</style>
