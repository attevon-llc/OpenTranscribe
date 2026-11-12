<!--
  EngineIntro.svelte — what the Speaker Engine tab is for, and whether the sidecar is up.

  One line says what the switch is (a runtime choice between engines the deployment already has,
  not an installer) beside a live read-only sidecar chip. The longer explanation and the
  administrator detail sit behind "How it works", closed by default. Nothing here writes anything.
-->
<script context="module" lang="ts">
  export type SidecarStatus = 'reachable' | 'unreachable' | 'unknown';
</script>

<script lang="ts">
  import { t } from '$stores/locale';
  import StatusChip from '$components/settings/StatusChip.svelte';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';

  /** Absent on a backend older than the field, which reads as "unknown", never as down. */
  export let sidecarStatus: SidecarStatus | undefined = undefined;

  const view: Record<SidecarStatus, { key: string; color: 'green' | 'red' | 'neutral' }> = {
    reachable: { key: 'settings.engineSettings.sidecarStatus.reachable', color: 'green' },
    unreachable: { key: 'settings.engineSettings.sidecarStatus.unreachable', color: 'red' },
    unknown: { key: 'settings.engineSettings.sidecarStatus.unknown', color: 'neutral' },
  };

  $: status = sidecarStatus ?? 'unknown';
</script>

<div class="engine-intro" data-testid="engine-intro">
  <div class="intro-head" data-testid="sidecar-status" data-status={status}>
    <StatusChip
      label={$t('settings.engineSettings.sidecarStatus.label')}
      value={$t(view[status].key)}
      status={view[status].color}
    />
  </div>
  <p class="summary">{$t('settings.engineSettings.intro.summary')}</p>
  <ExpandableSection title={$t('settings.engineSettings.intro.howItWorks')}>
    <p>{$t('settings.engineSettings.intro.details')}</p>
    <p class="admin-detail">{$t('settings.engineSettings.intro.overrideAdmin')}</p>
  </ExpandableSection>
</div>

<style>
  .engine-intro {
    padding: var(--space-3-5) var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--background-color);
    font-size: var(--font-size-md);
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .engine-intro p {
    margin: var(--space-2) 0 0;
  }

  .engine-intro p.summary {
    margin-top: var(--space-2);
    color: var(--text-color);
  }

  .engine-intro :global(.expandable) {
    margin-top: var(--space-2);
    border-bottom: none;
  }

  .engine-intro p.admin-detail {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .intro-head {
    display: flex;
    align-items: center;
  }
</style>
