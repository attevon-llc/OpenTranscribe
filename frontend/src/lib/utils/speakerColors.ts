// Curated pleasant color palette for speaker identification (expandable to 30+ speakers)
// Each color has both light and dark theme variants for optimal readability
const speakerColors = [
  // Primary set - most distinct and pleasant colors (first 15)
  {
    bg: 'rgba(99, 102, 241, 0.15)',
    border: 'rgba(99, 102, 241, 0.3)',
    textLight: '#4338ca', // Dark text for light mode
    textDark: '#a5b4fc', // Light text for dark mode
    solid: '#6366f1',
  }, // Indigo
  {
    bg: 'rgba(20, 184, 166, 0.15)',
    border: 'rgba(20, 184, 166, 0.3)',
    textLight: '#0f766e',
    textDark: '#5eead4',
    solid: '#14b8a6',
  }, // Teal
  {
    bg: 'rgba(245, 158, 11, 0.15)',
    border: 'rgba(245, 158, 11, 0.3)',
    textLight: '#d97706',
    textDark: '#fcd34d',
    solid: '#f59e0b',
  }, // Amber
  {
    bg: 'rgba(168, 85, 247, 0.15)',
    border: 'rgba(168, 85, 247, 0.3)',
    textLight: '#7c3aed',
    textDark: '#c4b5fd',
    solid: '#a855f7',
  }, // Purple
  {
    bg: 'rgba(6, 182, 212, 0.15)',
    border: 'rgba(6, 182, 212, 0.3)',
    textLight: '#0891b2',
    textDark: '#67e8f9',
    solid: '#06b6d4',
  }, // Cyan
  {
    bg: 'rgba(236, 72, 153, 0.15)',
    border: 'rgba(236, 72, 153, 0.3)',
    textLight: '#be185d',
    textDark: '#f9a8d4',
    solid: '#ec4899',
  }, // Rose
  {
    bg: 'rgba(34, 197, 134, 0.15)',
    border: 'rgba(34, 197, 134, 0.3)',
    textLight: '#059669',
    textDark: '#86efac',
    solid: '#22c586',
  }, // Emerald
  {
    bg: 'rgba(251, 146, 60, 0.15)',
    border: 'rgba(251, 146, 60, 0.3)',
    textLight: '#ea580c',
    textDark: '#fdba74',
    solid: '#fb923c',
  }, // Orange
  {
    bg: 'rgba(139, 123, 255, 0.15)',
    border: 'rgba(139, 123, 255, 0.3)',
    textLight: '#6366f1',
    textDark: '#c7d2fe',
    solid: '#8b7bff',
  }, // Lavender
  {
    bg: 'rgba(34, 211, 238, 0.15)',
    border: 'rgba(34, 211, 238, 0.3)',
    textLight: '#0369a1',
    textDark: '#7dd3fc',
    solid: '#22d3ee',
  }, // Sky Blue
  {
    bg: 'rgba(156, 163, 175, 0.15)',
    border: 'rgba(156, 163, 175, 0.3)',
    textLight: '#6b7280',
    textDark: '#d1d5db',
    solid: '#9ca3af',
  }, // Cool Gray
  {
    bg: 'rgba(52, 211, 153, 0.15)',
    border: 'rgba(52, 211, 153, 0.3)',
    textLight: '#047857',
    textDark: '#6ee7b7',
    solid: '#34d399',
  }, // Mint Green
  {
    bg: 'rgba(251, 113, 133, 0.15)',
    border: 'rgba(251, 113, 133, 0.3)',
    textLight: '#be123c',
    textDark: '#fca5a5',
    solid: '#fb7185',
  }, // Coral
  {
    bg: 'rgba(124, 58, 237, 0.15)',
    border: 'rgba(124, 58, 237, 0.3)',
    textLight: '#581c87',
    textDark: '#a78bfa',
    solid: '#7c3aed',
  }, // Deep Purple
  {
    bg: 'rgba(14, 165, 233, 0.15)',
    border: 'rgba(14, 165, 233, 0.3)',
    textLight: '#0369a1',
    textDark: '#60a5fa',
    solid: '#0ea5e9',
  }, // Bright Blue

  // Secondary set - lighter variations for speakers 16-30
  {
    bg: 'rgba(129, 140, 248, 0.15)',
    border: 'rgba(129, 140, 248, 0.3)',
    textLight: '#3730a3',
    textDark: '#c7d2fe',
    solid: '#818cf8',
  }, // Light Indigo
  {
    bg: 'rgba(45, 212, 191, 0.15)',
    border: 'rgba(45, 212, 191, 0.3)',
    textLight: '#0d9488',
    textDark: '#99f6e4',
    solid: '#2dd4bf',
  }, // Light Teal
  {
    bg: 'rgba(252, 176, 64, 0.15)',
    border: 'rgba(252, 176, 64, 0.3)',
    textLight: '#c2410c',
    textDark: '#fed7aa',
    solid: '#fcb040',
  }, // Light Amber
  {
    bg: 'rgba(196, 181, 253, 0.15)',
    border: 'rgba(196, 181, 253, 0.3)',
    textLight: '#6d28d9',
    textDark: '#ddd6fe',
    solid: '#c4b5fd',
  }, // Light Purple
  {
    bg: 'rgba(103, 232, 249, 0.15)',
    border: 'rgba(103, 232, 249, 0.3)',
    textLight: '#0c4a6e',
    textDark: '#a5f3fc',
    solid: '#67e8f9',
  }, // Light Cyan
  {
    bg: 'rgba(244, 114, 182, 0.15)',
    border: 'rgba(244, 114, 182, 0.3)',
    textLight: '#9d174d',
    textDark: '#fbcfe8',
    solid: '#f472b6',
  }, // Light Rose
  {
    bg: 'rgba(110, 231, 183, 0.15)',
    border: 'rgba(110, 231, 183, 0.3)',
    textLight: '#065f46',
    textDark: '#bbf7d0',
    solid: '#6ee7b7',
  }, // Light Emerald
  {
    bg: 'rgba(253, 186, 116, 0.15)',
    border: 'rgba(253, 186, 116, 0.3)',
    textLight: '#c2410c',
    textDark: '#fed7aa',
    solid: '#fdba74',
  }, // Light Orange
  {
    bg: 'rgba(165, 180, 252, 0.15)',
    border: 'rgba(165, 180, 252, 0.3)',
    textLight: '#3730a3',
    textDark: '#e0e7ff',
    solid: '#a5b4fc',
  }, // Light Lavender
  {
    bg: 'rgba(125, 211, 252, 0.15)',
    border: 'rgba(125, 211, 252, 0.3)',
    textLight: '#0369a1',
    textDark: '#bae6fd',
    solid: '#7dd3fc',
  }, // Light Sky
  {
    bg: 'rgba(209, 213, 219, 0.15)',
    border: 'rgba(209, 213, 219, 0.3)',
    textLight: '#4b5563',
    textDark: '#e5e7eb',
    solid: '#d1d5db',
  }, // Light Gray
  {
    bg: 'rgba(134, 239, 172, 0.15)',
    border: 'rgba(134, 239, 172, 0.3)',
    textLight: '#15803d',
    textDark: '#bbf7d0',
    solid: '#86efac',
  }, // Light Mint
  {
    bg: 'rgba(252, 165, 165, 0.15)',
    border: 'rgba(252, 165, 165, 0.3)',
    textLight: '#991b1b',
    textDark: '#fecaca',
    solid: '#fca5a5',
  }, // Light Coral
  {
    bg: 'rgba(167, 139, 250, 0.15)',
    border: 'rgba(167, 139, 250, 0.3)',
    textLight: '#5b21b6',
    textDark: '#ddd6fe',
    solid: '#a78bfa',
  }, // Light Deep Purple
  {
    bg: 'rgba(96, 165, 250, 0.15)',
    border: 'rgba(96, 165, 250, 0.3)',
    textLight: '#1e40af',
    textDark: '#bfdbfe',
    solid: '#60a5fa',
  }, // Light Bright Blue
];

/**
 * Get speaker color for transcript segments
 * Uses speaker_label which now ALWAYS contains the original "SPEAKER_##" ID
 * @param segment - Transcript segment object
 * @returns Color object with bg, border, text colors
 */
export function getSpeakerColorForSegment(segment: { speaker_label?: string | null }) {
  // speaker_label now consistently contains the original ID like "SPEAKER_01"
  const originalId = segment.speaker_label || 'Unknown';
  return getSpeakerColor(originalId);
}

/**
 * Get speaker color based on speaker name using consistent hashing
 * Scales robustly to 30+ speakers with curated pleasant colors
 * Returns colors with CSS custom properties for theme awareness
 * @param speakerName - The name of the speaker
 * @returns Object with background, border, text, and solid color values
 */
export function getSpeakerColor(speakerName: string) {
  if (!speakerName) {
    const color = speakerColors[0];
    return {
      bg: color.bg,
      border: color.border,
      textLight: color.textLight,
      textDark: color.textDark,
      solid: color.solid,
    };
  }

  // Create a consistent hash from speaker name
  let hash = 0;
  for (let i = 0; i < speakerName.length; i++) {
    const char = speakerName.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash = hash & hash; // Convert to 32-bit integer
  }

  const index = Math.abs(hash) % speakerColors.length;
  const color = speakerColors[index];

  return {
    bg: color.bg,
    border: color.border,
    textLight: color.textLight,
    textDark: color.textDark,
    solid: color.solid,
  };
}
