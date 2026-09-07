<script lang="ts">
  export let label: string;
  export let value: string;
  export let status: 'green' | 'yellow' | 'red' | 'blue' | 'neutral' = 'neutral';
</script>

<div class="status-chip" class:green={status === 'green'} class:yellow={status === 'yellow'} class:red={status === 'red'} class:blue={status === 'blue'}>
  <span class="chip-dot"></span>
  <span class="chip-label">{label}</span>
  <span class="chip-value">{value}</span>
</div>

<style>
  .status-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.3rem 0.625rem;
    border-radius: 16px;
    font-size: 0.75rem;
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    white-space: nowrap;
  }

  .chip-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-secondary);
  }

  .chip-label {
    color: var(--text-secondary);
    font-weight: 400;
  }

  .chip-value {
    color: var(--text-color);
    font-weight: 600;
  }

  /* Green */
  .status-chip.green {
    border-color: rgba(34, 197, 94, 0.3);
    background: rgba(34, 197, 94, 0.08);
  }
  .status-chip.green .chip-dot {
    background: #22c55e;
  }

  /* Yellow */
  .status-chip.yellow {
    border-color: rgba(251, 191, 36, 0.3);
    background: rgba(251, 191, 36, 0.08);
  }
  .status-chip.yellow .chip-dot {
    background: #f59e0b;
  }

  /* Red */
  .status-chip.red {
    border-color: rgba(239, 68, 68, 0.3);
    background: rgba(239, 68, 68, 0.08);
  }
  .status-chip.red .chip-dot {
    background: #ef4444;
  }

  /* Blue */
  .status-chip.blue {
    border-color: rgba(59, 130, 246, 0.3);
    background: rgba(59, 130, 246, 0.08);
  }
  .status-chip.blue .chip-dot {
    background: var(--primary-color);
  }
</style>
