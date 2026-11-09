import { describe, expect, it } from 'vitest';
import { optimalWaveformResolution, type WaveformEnvironment } from './waveformResolution';

const desktop: WaveformEnvironment = {
  containerWidth: 900,
  devicePixelRatio: 1,
  screenWidth: 1440,
};

describe('optimalWaveformResolution', () => {
  it.each([
    ['a phone-sized screen', { ...desktop, screenWidth: 600 }, 500],
    ['a narrow container on a desktop', { ...desktop, containerWidth: 599 }, 500],
    ['a tablet', { ...desktop, screenWidth: 900 }, 1000],
    ['a standard desktop', desktop, 1000],
    ['a wide container', { ...desktop, containerWidth: 1200 }, 2000],
    ['a high-DPI desktop', { ...desktop, devicePixelRatio: 2 }, 2000],
  ] as [string, WaveformEnvironment, number][])('%s gets %i peaks', (_name, env, expected) => {
    expect(optimalWaveformResolution(env)).toBe(expected);
  });

  it('uses the smallest size on a slow connection whatever the screen', () => {
    const wide = { ...desktop, containerWidth: 2000, devicePixelRatio: 3 };
    expect(optimalWaveformResolution({ ...wide, connection: { effectiveType: '2g' } })).toBe(500);
    expect(optimalWaveformResolution({ ...wide, connection: { effectiveType: 'slow-2g' } })).toBe(
      500
    );
    expect(optimalWaveformResolution({ ...wide, connection: { downlink: 1.4 } })).toBe(500);
  });

  it('ignores a fast or unreported connection', () => {
    expect(optimalWaveformResolution({ ...desktop, connection: { downlink: 10 } })).toBe(1000);
    expect(optimalWaveformResolution({ ...desktop, connection: { effectiveType: '4g' } })).toBe(
      1000
    );
    expect(optimalWaveformResolution({ ...desktop, connection: null })).toBe(1000);
  });
});
