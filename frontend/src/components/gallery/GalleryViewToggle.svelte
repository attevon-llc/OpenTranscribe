<script lang="ts">
  import { galleryStore, galleryViewMode, type ViewMode } from '$stores/gallery';
  import { t } from '$stores/locale';

  function setViewMode(mode: ViewMode) {
    galleryStore.setViewMode(mode);
  }

  function handleKeydown(event: KeyboardEvent, mode: ViewMode) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setViewMode(mode);
    }
    // Arrow key navigation between buttons
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const newMode = $galleryViewMode === 'grid' ? 'list' : 'grid';
      setViewMode(newMode);
      // Focus the other button
      const container = (event.target as HTMLElement).closest('.view-toggle');
      const targetButton = container?.querySelector(`[data-mode="${newMode}"]`) as HTMLElement;
      targetButton?.focus();
    }
  }
</script>

<div class="view-toggle" role="group" aria-label={$t('gallery.viewOptions')}>
  <button
    type="button"
    class="toggle-btn"
    class:active={$galleryViewMode === 'grid'}
    aria-pressed={$galleryViewMode === 'grid'}
    aria-label={$t('gallery.gridView')}
    title={$t('gallery.gridView')}
    data-mode="grid"
    on:click={() => setViewMode('grid')}
    on:keydown={(e) => handleKeydown(e, 'grid')}
  >
    <!-- Grid icon: 2x2 squares -->
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
    </svg>
  </button>

  <button
    type="button"
    class="toggle-btn"
    class:active={$galleryViewMode === 'list'}
    aria-pressed={$galleryViewMode === 'list'}
    aria-label={$t('gallery.listView')}
    title={$t('gallery.listView')}
    data-mode="list"
    on:click={() => setViewMode('list')}
    on:keydown={(e) => handleKeydown(e, 'list')}
  >
    <!-- List icon: horizontal lines -->
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <line x1="8" y1="6" x2="21" y2="6" />
      <line x1="8" y1="12" x2="21" y2="12" />
      <line x1="8" y1="18" x2="21" y2="18" />
      <line x1="3" y1="6" x2="3.01" y2="6" />
      <line x1="3" y1="12" x2="3.01" y2="12" />
      <line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  </button>
</div>

<style>
  .view-toggle {
    display: inline-flex;
    align-items: center;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 0.1875rem;
    height: 30px;
    box-sizing: border-box;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
    backdrop-filter: blur(8px);
  }

  :global([data-theme='light']) .view-toggle,
  :global(:not([data-theme='dark'])) .view-toggle {
    background: rgba(255, 255, 255, 0.9);
    border-color: rgba(0, 0, 0, 0.08);
  }

  :global([data-theme='dark']) .view-toggle {
    background: rgba(30, 41, 59, 0.95);
    border-color: rgba(255, 255, 255, 0.1);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  }

  .toggle-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s ease;
    color: var(--text-secondary);
  }

  :global([data-theme='light']) .toggle-btn,
  :global(:not([data-theme='dark'])) .toggle-btn {
    color: #6b7280;
  }

  :global([data-theme='dark']) .toggle-btn {
    color: #cbd5e1;
  }

  .toggle-btn:hover:not(.active) {
    background: var(--hover-bg);
    color: var(--text-primary);
  }

  :global([data-theme='light']) .toggle-btn:hover:not(.active),
  :global(:not([data-theme='dark'])) .toggle-btn:hover:not(.active) {
    background: rgba(0, 0, 0, 0.05);
    color: #1f2937;
  }

  :global([data-theme='dark']) .toggle-btn:hover:not(.active) {
    background: rgba(255, 255, 255, 0.1);
    color: #f9fafb;
  }

  .toggle-btn.active {
    background: var(--primary-color);
    color: white;
  }

  .toggle-btn:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .toggle-btn:focus:not(:focus-visible) {
    outline: none;
  }

  .toggle-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .toggle-btn svg {
    flex-shrink: 0;
  }

  /* Touch devices: ensure good touch targets within the fixed height */
  @media (pointer: coarse) {
    .toggle-btn {
      min-width: 26px;
      min-height: 26px;
    }
  }

  @media (max-width: 768px) {
    .view-toggle {
      padding: 0.15rem;
    }

    .toggle-btn {
      width: 22px;
      height: 22px;
      min-width: 22px;
      min-height: 22px;
    }
  }
</style>
