import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import SystemStatisticsPanel from './SystemStatisticsPanel.svelte';
import { initialSystemStats, type SystemStats } from '$lib/settings/adminLoaders';

const identity = (key: string) => key;

function renderWith(system: Record<string, unknown>) {
  const base = initialSystemStats(identity);
  const stats = { ...base, system: { ...base.system, ...system } } as SystemStats;
  return render(SystemStatisticsPanel, { stats, statsLoading: false });
}

// Usage gauges are bounded values with no completion, so WAI-ARIA role=meter, not progressbar.
describe('SystemStatisticsPanel usage meters', () => {
  it('exposes CPU, memory, disk and GPU VRAM as named meters with a 0-100 range', () => {
    const { container } = renderWith({
      cpu: { total_percent: '37.5%' },
      memory: { percent: '61%' },
      disk: { percent: '88%' },
      gpus: [{ available: true, memory_percent: '12%' }],
    });
    const meters = [...container.querySelectorAll<HTMLElement>('[role="meter"]')];
    expect(meters.map((m) => m.getAttribute('aria-label'))).toEqual([
      'settings.statistics.cpuUsage',
      'settings.statistics.memoryUsage',
      'settings.statistics.diskUsage',
      'settings.statistics.gpuVram',
    ]);
    expect(meters.map((m) => m.getAttribute('aria-valuenow'))).toEqual(['37.5', '61', '88', '12']);
    for (const m of meters) {
      expect(m.getAttribute('aria-valuemin')).toBe('0');
      expect(m.getAttribute('aria-valuemax')).toBe('100');
    }
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(meters[2].querySelector<HTMLElement>('.meter-fill')!.style.width).toBe('88%');
  });

  it('reads missing or unparsable usage as 0 and clamps above 100', () => {
    const { container } = renderWith({
      cpu: { total_percent: 'N/A' },
      memory: { percent: '250%' },
      disk: {},
      gpus: [],
    });
    const values = [...container.querySelectorAll<HTMLElement>('[role="meter"]')].map((m) =>
      m.getAttribute('aria-valuenow')
    );
    expect(values).toEqual(['0', '100', '0']);
  });
});
