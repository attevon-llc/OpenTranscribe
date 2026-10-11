<!--
  EngineIntro.svelte — what the Speaker Engine controls actually do, and whether the sidecar is up.

  The switch below is easy to misread as an installer. It is a runtime routing choice between two
  engines the deployment already has, so this says so up front, with a live read-only sidecar chip
  (from the engine-settings GET) beside it. Nothing here writes anything.
-->
<script context="module" lang="ts">
  export type SidecarStatus = 'reachable' | 'unreachable' | 'unknown';
</script>

<script lang="ts">
  import { t } from '$stores/locale';
  import StatusChip from '$components/settings/StatusChip.svelte';

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
  <p>{$t('settings.engineSettings.intro.routing')}</p>
  <p>{$t('settings.engineSettings.intro.override')}</p>
  <p>{$t('settings.engineSettings.intro.requireSidecar')}</p>
</div>

<style>
  .engine-intro {
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--background-color);
    font-size: 0.85rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .engine-intro p {
    margin: 0.5rem 0 0;
  }

  .intro-head {
    display: flex;
    align-items: center;
  }
</style>
