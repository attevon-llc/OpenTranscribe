import { describe, it, expect } from 'vitest';
import { badgeVariantForStatus } from './statusVariant';

describe('badgeVariantForStatus', () => {
  it.each([
    ['completed', 'success'],
    ['in_progress', 'info'],
    ['processing', 'info'],
    ['pending', 'warning'],
    ['queued', 'warning'],
    ['failed', 'error'],
    ['error', 'error'],
    ['cancelled', 'default'],
    ['something-new', 'default'],
  ])('maps %s to %s', (status, variant) => {
    expect(badgeVariantForStatus(status)).toBe(variant);
  });

  it('reads the backend status_badge_class the same as the raw status', () => {
    expect(badgeVariantForStatus('status-completed')).toBe('success');
    expect(badgeVariantForStatus('status-processing')).toBe('info');
    expect(badgeVariantForStatus('status-unknown')).toBe('default');
  });

  it('treats a missing status as the default pill', () => {
    expect(badgeVariantForStatus(null)).toBe('default');
    expect(badgeVariantForStatus(undefined)).toBe('default');
    expect(badgeVariantForStatus('')).toBe('default');
  });
});
