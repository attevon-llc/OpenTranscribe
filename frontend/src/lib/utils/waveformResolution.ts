/** What the waveform player knows about its device when it picks how many peaks to fetch. */
export interface WaveformEnvironment {
  containerWidth: number;
  devicePixelRatio: number;
  screenWidth: number;
  /** `navigator.connection`, where the browser exposes it. */
  connection?: { effectiveType?: string; downlink?: number } | null;
}

const SMALL = 500;
const MEDIUM = 1000;
const LARGE = 2000;

// A slow connection fetches the smallest waveform regardless of screen size.
function isLowBandwidth(connection: WaveformEnvironment['connection']): boolean {
  if (!connection) return false;
  const slowType = connection.effectiveType === 'slow-2g' || connection.effectiveType === '2g';
  // Less than 1.5 Mbps
  return slowType || (connection.downlink ?? Infinity) < 1.5;
}

/**
 * Waveform resolution (number of peaks) for the current device: bandwidth and screen size first,
 * then pixel density and container width.
 */
export function optimalWaveformResolution(env: WaveformEnvironment): number {
  const { containerWidth, devicePixelRatio, screenWidth } = env;
  if (isLowBandwidth(env.connection)) return SMALL;

  const isMobile = screenWidth < 768;
  const isTablet = screenWidth >= 768 && screenWidth < 1024;
  const isHighDPI = devicePixelRatio >= 2;

  // Small: mobile devices, small containers
  if (isMobile || containerWidth < 600) return SMALL;
  // Medium: tablets, standard desktop displays
  if (isTablet || (!isHighDPI && containerWidth < 1200)) return MEDIUM;
  // Large: high-DPI displays, large screens, detailed view
  if (isHighDPI || containerWidth >= 1200) return LARGE;
  return MEDIUM;
}
