<script lang="ts">
  import { t } from '$stores/locale';
  import GroupsOverview from '$components/groups/GroupsOverview.svelte';
</script>

<div class="groups-settings">
  <h3 class="section-title">{$t('groups.title')}</h3>
  <p class="section-description">{$t('groups.settingsDescription')}</p>
  <GroupsOverview />
</div>

<style>
  .groups-settings {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0;
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0;
    line-height: 1.5;
  }
</style>
