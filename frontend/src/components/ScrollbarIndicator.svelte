<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import {
    calculateScrollbarPositionBySegment,
    findCurrentSegment,
    createThrottledPositionUpdate,
    type TranscriptSegment
  } from '$lib/utils/scrollbarCalculations';
  import { t } from '$stores/locale';

  const dispatch = createEventDispatcher();

  // Props
  export let currentTime: number = 0;
  export let transcriptSegments: TranscriptSegment[] = [];
  export let containerElement: HTMLElement | Element | null = null;
  export let disabled: boolean = false;
  export let showTooltip: boolean = true;

  // State
  let indicatorElement: HTMLElement | null = null;
  let isVisible: boolean = false;
  let tooltipVisible: boolean = false;
  let tooltipText: string = '';
  let indicatorPosition: number = 0;
  let containerHeight: number = 600;
  let scrollbarWidth: number = 16;
  let overlayElement: HTMLElement | null = null;

  // Throttled position update to prevent excessive DOM manipulation
  const throttledPositionUpdate = createThrottledPositionUpdate((position: number) => {
    indicatorPosition = position;
  }, 32); // ~30fps for better performance with long transcripts

  // Reactive calculations
  $: {
    if (transcriptSegments && transcriptSegments.length > 0 && currentTime >= 0) {
      const position = calculateScrollbarPositionBySegment(currentTime, transcriptSegments);
      throttledPositionUpdate(position);

      // Show indicator only if we have valid content and position
      isVisible = position >= 0 && position <= 100;

      // Update tooltip text
      if (showTooltip) {
        const currentSegment = findCurrentSegment(currentTime, transcriptSegments);
        tooltipText = currentSegment
          ? `${formatTime(currentTime)} - ${currentSegment.text.substring(0, 40)}${currentSegment.text.length > 40 ? '...' : ''}\n\n${$t('scrollbar.clickToScroll')}`
          : `${formatTime(currentTime)}\n\n${$t('scrollbar.clickToScroll')}`;
      }
    } else {
      isVisible = false;
      indicatorPosition = 0;
    }
  }

  // Handle container size changes
  $: if (containerElement) {
    updateContainerDimensions();
  }

  function updateContainerDimensions() {
    if (containerElement && overlayElement) {
      // Get the actual height of the transcript-display container
      const rect = containerElement.getBoundingClientRect();
      containerHeight = rect.height || 600; // Use actual height or fallback to 600px

      // Position the overlay to match the exact height of transcript-display
      overlayElement.style.setProperty('--transcript-height', `${containerHeight}px`);

      // Detect scrollbar width dynamically
      if (containerElement instanceof HTMLElement) {
        const hasVerticalScrollbar = containerElement.scrollHeight > containerElement.clientHeight;
        scrollbarWidth = hasVerticalScrollbar ?
          containerElement.offsetWidth - containerElement.clientWidth : 16; // Default scrollbar width
      } else {
        scrollbarWidth = 16; // Default scrollbar width for Element
      }
    }
  }

  function formatTime(seconds: number): string {
    if (isNaN(seconds) || seconds < 0) return '0:00';

    const minutes = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${minutes}:${secs.toString().padStart(2, '0')}`;
  }

  function handleIndicatorClick(event: MouseEvent) {
    if (disabled || !transcriptSegments.length) return;

    event.preventDefault();
    event.stopPropagation();

    // Dispatch event to scroll transcript to current playhead position
    dispatch('seekToPlayhead', {
      currentTime,
      targetSegment: findCurrentSegment(currentTime, transcriptSegments)
    });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (disabled) return;

    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handleIndicatorClick(event as any);
    }
  }

  function showTooltipHandler() {
    if (showTooltip && !disabled) {
      tooltipVisible = true;
    }
  }

  function hideTooltipHandler() {
    tooltipVisible = false;
  }

  // Handle resize events
  let resizeObserver: ResizeObserver | null = null;

  onMount(() => {
    if (containerElement && window.ResizeObserver) {
      resizeObserver = new ResizeObserver(() => {
        updateContainerDimensions();
      });
      resizeObserver.observe(containerElement);
    }

    // Fallback for older browsers
    window.addEventListener('resize', updateContainerDimensions);
  });

  onDestroy(() => {
    if (resizeObserver) {
      resizeObserver.disconnect();
    }
    window.removeEventListener('resize', updateContainerDimensions);
  });
</script>

<!-- Scrollbar Indicator Overlay -->
{#if isVisible && !disabled}
  <div
    bind:this={overlayElement}
    class="scrollbar-indicator-overlay"
    aria-hidden="true"
  >
    <div
      bind:this={indicatorElement}
      class="playhead-indicator"
      class:has-tooltip={showTooltip}
      style="top: {indicatorPosition}%;"
      role="button"
      tabindex="0"
      aria-label={$t('scrollbar.ariaLabel', { time: formatTime(currentTime) })}
      title={showTooltip ? tooltipText : ''}
      on:click={handleIndicatorClick}
      on:keydown={handleKeydown}
      on:mouseenter={showTooltipHandler}
      on:mouseleave={hideTooltipHandler}
      on:focus={showTooltipHandler}
      on:blur={hideTooltipHandler}
    >
      <!-- Tooltip -->
      {#if tooltipVisible && showTooltip && tooltipText}
        <div class="indicator-tooltip">
          {tooltipText}
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .scrollbar-indicator-overlay {
    position: absolute;
    top: 0; /* Align with the top of transcript-display */
    right: -24px; /* Position just outside the transcript-display */
    width: 20px;
    height: var(--transcript-height, 600px); /* Dynamic height matching transcript-display exactly */
    pointer-events: none;
    z-index: 200; /* Higher z-index to appear above other elements */
    overflow: visible;
  }

  /* Add a subtle track background to show the indicator area */
  .scrollbar-indicator-overlay::before {
    content: '';
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
    top: 0;
    width: 2px;
    height: 100%;
    background: var(--border-color);
    opacity: 0.3;
    border-radius: 1px;
  }

  .playhead-indicator {
    position: absolute;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 20px; /* Touch target */
    height: 12px; /* Touch target */
    pointer-events: auto;
    cursor: pointer;
    transition: all 0.2s ease-out;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .playhead-indicator::before {
    content: '';
    position: absolute;
    width: 12px;
    height: 4px;
    background: var(--primary-color);
    border-radius: 2px;
    opacity: 0.9;
    transition: all 0.2s ease-out;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }

  .playhead-indicator:hover,
  .playhead-indicator:focus {
    opacity: 1;
    outline: none;
  }

  .playhead-indicator:hover::before,
  .playhead-indicator:focus::before {
    width: 14px;
    height: 6px;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
    opacity: 1;
  }

  .playhead-indicator:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* Dark theme adjustments */
  :global([data-theme='dark']) .playhead-indicator::before {
    background: var(--primary-color);
    opacity: 0.9;
  }

  :global([data-theme='dark']) .playhead-indicator:hover::before,
  :global([data-theme='dark']) .playhead-indicator:focus::before {
    opacity: 1;
    box-shadow: 0 2px 6px rgba(96, 165, 250, 0.4);
  }

  /* Tooltip styling */
  .indicator-tooltip {
    position: absolute;
    right: 30px;
    top: 50%;
    transform: translateY(-50%);
    background: var(--surface-color);
    color: var(--text-primary);
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 500;
    white-space: pre-line; /* Allow line breaks */
    max-width: 320px;
    min-width: 200px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    border: 1px solid var(--border-color);
    z-index: 1000;
    line-height: 1.4;

    /* Tooltip arrow */
    &::before {
      content: '';
      position: absolute;
      left: -8px;
      top: 50%;
      transform: translateY(-50%);
      width: 0;
      height: 0;
      border-top: 8px solid transparent;
      border-bottom: 8px solid transparent;
      border-right: 8px solid var(--surface-color);
    }
  }

  /* Hide scrollbar indicator on very small screens */
  @media (max-width: 480px) {
    .scrollbar-indicator-overlay {
      display: none;
    }
  }

  /* Reduce motion for users who prefer it */
  @media (prefers-reduced-motion: reduce) {
    .playhead-indicator,
    .playhead-indicator::before {
      transition: none;
    }
  }

  /* High contrast mode support */
  @media (prefers-contrast: high) {
    .playhead-indicator::before {
      border: 1px solid;
      background: CanvasText;
    }

    .indicator-tooltip {
      border: 2px solid;
      background: Canvas;
      color: CanvasText;
    }
  }

  /* Ensure proper focus visibility in all browsers */
  .playhead-indicator:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* Handle edge case where indicator might be at very top or bottom */
  .playhead-indicator[style*="top: 0%"] {
    transform: translate(-50%, 0);
  }

  .playhead-indicator[style*="top: 100%"] {
    transform: translate(-50%, -100%);
  }

  /* Ensure indicator is visible on touch devices */
  @media (hover: none) and (pointer: coarse) {
    .playhead-indicator {
      width: 24px;
      height: 16px;
      /* Keep the same left: 50%, transform centering */
    }

    .playhead-indicator::before {
      width: 16px;
      height: 6px;
    }

    .indicator-tooltip {
      display: none; /* Hide tooltips on touch devices */
    }
  }
</style>
