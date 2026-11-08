import { describe, it, expect } from 'vitest';
import { translateFileStatus } from './statusLabel';

const t = (key: string) => `[${key}]`;

describe('translateFileStatus', () => {
  it('picks the label from the status code, ignoring the English display text', () => {
    expect(translateFileStatus(t, 'completed', 'Completed')).toBe('[common.completed]');
    expect(translateFileStatus(t, 'In Progress', '')).toBe('[fileStatus.inProgress]');
  });
  it('falls back to the server text for a status it has no key for', () => {
    expect(translateFileStatus(t, 'mystery', 'Mystery')).toBe('Mystery');
    expect(translateFileStatus(t, null, 'Unknown')).toBe('Unknown');
  });
});
