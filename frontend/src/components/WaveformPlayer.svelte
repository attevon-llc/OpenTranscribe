<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { theme } from '../stores/theme.js';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from './ui/Spinner.svelte';

  // Props
  export let fileId: string | number;
  export let duration: number = 0;
  export let currentTime: number = 0;
  export let height: number = 80;

  // State
  let canvas: HTMLCanvasElement;
  let container: HTMLDivElement;
  let ctx: CanvasRenderingContext2D;
  let waveformData: number[] = [];
  let isLoadingWaveform = false;
  let waveformError: string = '';
  let isDragging = false;
  let animationFrameId: number | null = null;
  // A click/keyboard seek that arrives before `duration` is known (e.g. the
  // player hasn't reported its duration yet) used to be silently discarded —
  // the interaction had no effect and gave no feedback. Queue it and flush
  // once `duration` becomes available instead (#649).
  let pendingSeekRatio: number | null = null;

  const dispatch = createEventDispatcher();

  // Waveform colors - using CSS custom properties for theming
  const getWaveformColors = () => {
    const computedStyle = getComputedStyle(document.documentElement);
    return {
      background: computedStyle.getPropertyValue('--surface-color').trim() || '#f8fafc',
      waveform: computedStyle.getPropertyValue('--text-secondary').trim() || '#64748b',
      progress: computedStyle.getPropertyValue('--primary-color').trim() || 'var(--primary-color)',
      playhead: computedStyle.getPropertyValue('--primary-color').trim() || '#3b82f6'
    };
  };

  /**
   * Determine optimal waveform resolution based on device and screen size
   */
  function getOptimalResolution() {
    const containerWidth = container?.offsetWidth || 800;
    const devicePixelRatio = window.devicePixelRatio || 1;
    const screenWidth = window.innerWidth;

    // Check for low bandwidth connections
    const connection = (navigator as any).connection;
    const isLowBandwidth = connection && (
      connection.effectiveType === 'slow-2g' ||
      connection.effectiveType === '2g' ||
      connection.downlink < 1.5  // Less than 1.5 Mbps
    );

    const isMobile = screenWidth < 768;
    const isTablet = screenWidth >= 768 && screenWidth < 1024;
    const isHighDPI = devicePixelRatio >= 2;
    const isLargeContainer = containerWidth >= 1200;

    // Prioritize bandwidth and device constraints
    if (isLowBandwidth) {
      return 500;  // Always use smallest for poor connections
    }

    // Choose resolution based on device and screen characteristics
    if (isMobile || containerWidth < 600) {
      return 500;  // Small: mobile devices, small containers
    } else if (isTablet || (!isHighDPI && containerWidth < 1200)) {
      return 1000; // Medium: tablets, standard desktop displays
    } else if (isHighDPI || isLargeContainer) {
      return 2000; // Large: high-DPI displays, large screens, detailed view
    } else {
      return 1000; // Default to medium for standard cases
    }
  }

  /**
   * Load waveform data from the backend API
   */
  async function loadWaveformData() {
    if (!fileId || isLoadingWaveform) return;

    try {
      isLoadingWaveform = true;
      waveformError = '';

      // Choose optimal resolution for current device/screen
      const targetSamples = getOptimalResolution();

      const response = await axiosInstance.get(`/files/${fileId}/waveform`, {
        params: { samples: targetSamples }
      });

      if (response.data && Array.isArray(response.data.waveform) && response.data.waveform.length > 0) {
        waveformData = response.data.waveform;

        // Use the actual extracted duration from waveform if available
        // This ensures waveform aligns with actual audio content
        if (response.data.duration) {
          // Only update if significantly different or not set
          if (!duration || Math.abs(duration - response.data.duration) > 0.1) {
            duration = response.data.duration;
          }
        }

        // Ensure canvas and context are ready
        if (canvas && container) {
          if (!ctx) {
            ctx = canvas.getContext('2d')!;
            handleResize();
          }
          drawWaveform();
        }
      } else {
        throw new Error($t('waveform.noData'));
      }
    } catch (error: unknown) {
      console.error('Error loading waveform:', error);
      waveformError = getErrorMessage(error, $t('waveform.loadFailed'));
    } finally {
      isLoadingWaveform = false;
    }
  }

  /**
   * Draw the waveform visualization
   */
  function drawWaveform() {
    if (!ctx || !canvas || waveformData.length === 0) {
      return;
    }

    const canvasWidth = canvas.width;
    const canvasHeight = canvas.height;

    if (canvasWidth === 0 || canvasHeight === 0) {
      return;
    }

    const colors = getWaveformColors();

    // Clear canvas
    ctx.clearRect(0, 0, canvasWidth, canvasHeight);

    // Draw background
    ctx.fillStyle = colors.background;
    ctx.fillRect(0, 0, canvasWidth, canvasHeight);

    // Calculate dimensions to fill entire width
    const centerY = canvasHeight / 2;
    const maxBarHeight = canvasHeight * 0.8;
    const barWidth = canvasWidth / waveformData.length; // Use exact division, no floor
    const actualBarWidth = Math.max(0.5, barWidth * 0.9); // Ensure bars are visible
    const gapWidth = barWidth * 0.1; // Small gap between bars

    // Calculate progress position - ensure it's within bounds
    const progressX = duration > 0 ? Math.min((currentTime / duration) * canvasWidth, canvasWidth) : 0;

    // Draw waveform bars
    waveformData.forEach((sample, index) => {
      const x = index * barWidth;

      // Ensure sample value is valid
      const normalizedSample = Math.min(255, Math.max(0, sample));
      const barHeight = Math.max(2, (normalizedSample / 255) * maxBarHeight);
      const y = centerY - (barHeight / 2);

      // Use different colors for played vs unplayed portions
      // Check the center of the bar for more accurate coloring
      const barCenterX = x + (barWidth / 2);
      const isPlayed = barCenterX < progressX;
      ctx.fillStyle = isPlayed ? colors.progress : colors.waveform;

      // Draw the bar - use exact positioning to fill width
      ctx.fillRect(x + gapWidth/2, y, actualBarWidth, barHeight);
    });

    // Draw playhead indicator
    if (progressX > 0 && progressX <= canvasWidth) {
      ctx.strokeStyle = colors.playhead;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(progressX, 0);
      ctx.lineTo(progressX, canvasHeight);
      ctx.stroke();
    }
  }

  /**
   * Handle canvas resize
   */
  function handleResize() {
    if (!container || !canvas) return;

    const containerWidth = container.offsetWidth;

    // Set canvas size to match container exactly
    canvas.width = containerWidth;
    canvas.height = height;

    // Set display size to match container exactly
    canvas.style.width = `${containerWidth}px`;
    canvas.style.height = `${height}px`;

    // Get fresh context
    ctx = canvas.getContext('2d')!;

    drawWaveform();
  }

  /**
   * A real mouse click fires mousedown AND click, and both used to seek — so
   * every click on the waveform issued two identical seeks to the media element.
   * `handleMouseDown` claims the interaction and sets this flag so the trailing
   * `click` is a no-op. The `on:click` handler stays because synthetic clicks
   * (assistive tech, tests) arrive without a preceding mousedown.
   */
  let mouseDownHandledSeek = false;

  function seekFromPointer(event: MouseEvent) {
    if (!container) return;

    const rect = container.getBoundingClientRect();
    const clickX = event.clientX - rect.left;
    const seekRatio = Math.min(Math.max(clickX / rect.width, 0), 1);

    if (duration <= 0) {
      // Duration isn't known yet (e.g. clicked immediately on a fresh page
      // load, before the player has reported it). Queue the ratio rather than
      // discarding the click — the reactive block below flushes it as soon as
      // `duration` arrives.
      pendingSeekRatio = seekRatio;
      return;
    }

    dispatch('seek', { time: seekRatio * duration });
  }

  // Flush a seek that arrived before duration was known.
  $: if (duration > 0 && pendingSeekRatio !== null) {
    const ratio = pendingSeekRatio;
    pendingSeekRatio = null;
    dispatch('seek', { time: ratio * duration });
  }

  /**
   * Handle click/seek on waveform
   */
  function handleInteraction(event: MouseEvent) {
    if (mouseDownHandledSeek) {
      mouseDownHandledSeek = false;
      return;
    }
    seekFromPointer(event);
  }

  /**
   * Handle mouse down for dragging
   */
  function handleMouseDown(event: MouseEvent) {
    isDragging = true;
    mouseDownHandledSeek = true;
    seekFromPointer(event);

    // Coalesce drag updates to one seek per frame. Raw mousemove fires far
    // faster than the media element can service a seek, and each one can cost a
    // fresh byte-range request.
    let pendingMove: MouseEvent | null = null;
    let rafId: number | null = null;

    const flushMove = () => {
      rafId = null;
      if (pendingMove && isDragging) {
        seekFromPointer(pendingMove);
      }
      pendingMove = null;
    };

    const handleMouseMove = (moveEvent: MouseEvent) => {
      if (!isDragging) return;
      pendingMove = moveEvent;
      if (rafId === null) {
        rafId = requestAnimationFrame(flushMove);
      }
    };

    const handleMouseUp = () => {
      isDragging = false;
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      pendingMove = null;
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }

  /**
   * Handle keyboard navigation
   */
  function handleKeyDown(event: KeyboardEvent) {
    // 'Home' always means "seek to 0" regardless of whether duration is known
    // yet, so it doesn't need to wait. The other keys need `duration` to
    // compute a target and are dropped silently below — same call as the
    // click case above, but they act on a live `currentTime` rather than a
    // ratio, so there's no meaningful value to queue when duration is still 0.
    if (duration <= 0 && event.code !== 'Home') return;

    let seekTime = currentTime;
    const seekStep = duration * 0.01; // 1% of duration

    switch (event.code) {
      case 'ArrowLeft':
        seekTime = Math.max(0, currentTime - seekStep);
        break;
      case 'ArrowRight':
        seekTime = Math.min(duration, currentTime + seekStep);
        break;
      case 'Home':
        seekTime = 0;
        break;
      case 'End':
        seekTime = duration;
        break;
      default:
        return;
    }

    event.preventDefault();
    dispatch('seek', { time: seekTime });
  }

  // Reactive updates - redraw when waveform data or playhead position changes
  // Use currentTime directly in the expression to ensure it's tracked as a dependency
  $: if (canvas && ctx && waveformData.length > 0) {
    // Track currentTime changes by referencing it
    const _ = currentTime;
    drawWaveform();
  }

  // Redraw when theme changes to pick up new CSS custom property values
  $: if (canvas && ctx && waveformData.length > 0 && $theme) {
    drawWaveform();
  }

  // Mount and cleanup
  onMount(() => {
    // Svelte's `bind:this` assigns `canvas`/`container` before `onMount`
    // fires, so the DOM is already there — the previous 100ms sleep bought
    // nothing but delayed `loadWaveformData()`, which doesn't touch the DOM
    // at all (getOptimalResolution reads `container.offsetWidth`, which is
    // already valid here). Waveform fetches on a fresh page load used to be
    // dead-weighted by this (#649); it also skewed getOptimalResolution's
    // container-width check on the very first paint.
    if (canvas && container) {
      handleResize();
    }
    loadWaveformData();

    // Cleanup function
    return () => {
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
      }
    };
  });

  onDestroy(() => {
    if (animationFrameId) {
      cancelAnimationFrame(animationFrameId);
    }
  });
</script>

<div class="waveform-container" bind:this={container}>
  <!-- Always render canvas so binding works -->
  <canvas
    bind:this={canvas}
    class="waveform-canvas"
    class:hidden={isLoadingWaveform || waveformError || waveformData.length === 0}
    on:click={handleInteraction}
    on:mousedown={handleMouseDown}
    on:keydown={handleKeyDown}
    tabindex="0"
    role="slider"
    aria-label={$t('waveform.ariaLabel')}
    aria-valuenow={currentTime}
    aria-valuemin="0"
    aria-valuemax={duration}
    aria-valuetext={`${Math.floor(currentTime / 60)}:${(currentTime % 60).toFixed(0).padStart(2, '0')}`}
  ></canvas>

  <!-- Overlay states -->
  {#if isLoadingWaveform}
    <div class="waveform-overlay waveform-loading">
      <Spinner size="medium" />
      <span>{$t('waveform.loading')}</span>
    </div>
  {:else if waveformError}
    <div class="waveform-overlay waveform-error">
      <span>{waveformError}</span>
      <button
        class="retry-button"
        on:click={loadWaveformData}
        type="button"
      >
        {$t('waveform.retry')}
      </button>
    </div>
  {:else if waveformData.length === 0}
    <div class="waveform-overlay waveform-placeholder">
      <span>{$t('waveform.notAvailable')}</span>
    </div>
  {/if}
</div>

<style>
  .waveform-container {
    position: relative;
    width: 100%;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
    cursor: pointer;
    user-select: none;
  }

  .waveform-canvas {
    width: 100%;
    display: block;
    cursor: crosshair;
  }

  .waveform-canvas.hidden {
    visibility: hidden;
  }

  .waveform-canvas:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .waveform-canvas:hover {
    background-color: var(--background-color);
  }

  .waveform-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    font-size: 14px;
    gap: 0.5rem;
    background-color: var(--surface-color);
  }


  .waveform-error {
    flex-direction: column;
    color: var(--error-color);
  }

  .retry-button {
    padding: 0.25rem 0.75rem;
    background-color: var(--primary-color);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.875rem;
    transition: background-color 0.2s ease;
  }

  .retry-button:hover {
    background-color: #2563eb;
  }

  .retry-button:active {
    transform: translateY(1px);
  }

  /* Dark mode support using theme attribute */
  :global([data-theme='dark']) .waveform-container {
    background-color: var(--surface-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .waveform-canvas:hover {
    background-color: var(--background-color);
  }

  /* Responsive adjustments */
  @media (max-width: 768px) {
    .waveform-container {
      border-radius: 4px;
    }

    .waveform-loading,
    .waveform-error,
    .waveform-placeholder {
      font-size: 13px;
      height: 60px;
    }
  }
</style>
