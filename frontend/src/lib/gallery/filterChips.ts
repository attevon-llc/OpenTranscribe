/**
 * Pure helpers behind the filter sidebar's chips and toggles.
 *
 * Wire format for durations is seconds (the gallery query builds min_duration/max_duration from the
 * same range). `null` at a bound means "no limit", the convention the slider and the chips share so
 * a chip click and a slider drag never disagree.
 */

export interface DurationBounds {
  min: number;
  max: number;
}
export interface DurationRangeValue {
  min: number | null;
  max: number | null;
}
export interface DurationChip {
  code: string;
  min: number;
  max: number | null;
}

/** Duration quick chips (#750 item 8): four named buckets with fixed second ranges. */
export const DURATION_CHIPS: DurationChip[] = [
  { code: 'duration1to10', min: 60, max: 600 },
  { code: 'duration11to30', min: 660, max: 1800 },
  { code: 'duration31to60', min: 1860, max: 3600 },
  { code: 'duration60plus', min: 3600, max: null },
];

/** Add the item when absent, remove it when present. */
export function toggleItem(list: string[], item: string): string[] {
  return list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
}

/** Clamp a chip's nominal range into the library's real bounds. */
export function chipRange(chip: DurationChip, bounds: DurationBounds): [number, number] {
  const min = Math.max(chip.min, bounds.min);
  const max = chip.max === null ? bounds.max : Math.min(chip.max, bounds.max);
  return [min, max];
}

/**
 * A chip that provably matches nothing in this library is the same class of defect #744 fixed, so
 * only chips whose nominal range overlaps the real bounds are shown.
 */
export function visibleDurationChips(bounds: DurationBounds): DurationChip[] {
  return DURATION_CHIPS.filter(
    (chip) => chip.min <= bounds.max && (chip.max === null || chip.max >= bounds.min)
  );
}

/** Slider handles to a range, with a handle resting on its bound meaning "unbounded" (null). */
export function rangeFromValues(
  min: number,
  max: number,
  bounds: DurationBounds
): DurationRangeValue {
  return {
    min: min <= bounds.min ? null : min,
    max: max >= bounds.max ? null : max,
  };
}

export function isDurationChipActive(
  chip: DurationChip,
  bounds: DurationBounds,
  range: DurationRangeValue
): boolean {
  const [min, max] = chipRange(chip, bounds);
  const eff = rangeFromValues(min, max, bounds);
  return range.min === eff.min && range.max === eff.max;
}
