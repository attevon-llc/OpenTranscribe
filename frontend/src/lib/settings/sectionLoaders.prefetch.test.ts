import { beforeEach, describe, expect, it, vi } from 'vitest';

const queued = vi.hoisted(() => ({ tasks: [] as Array<() => unknown>, cancel: vi.fn() }));
vi.mock('$lib/settings/idleQueue', () => ({
  runIdleQueue: vi.fn((tasks: Array<() => unknown>) => {
    queued.tasks = tasks;
    return queued.cancel;
  }),
}));
const dataMock = vi.hoisted(() => ({ prefetch: vi.fn(), has: vi.fn() }));
vi.mock('$lib/settings/sectionData', () => ({
  prefetchSectionData: dataMock.prefetch,
  hasSectionPrefetch: dataMock.has,
}));

import { connectionAllowsPrefetch, startIdlePrefetch } from './sectionLoaders';
import { runIdleQueue } from '$lib/settings/idleQueue';

function setConnection(value: unknown) {
  Object.defineProperty(navigator, 'connection', { value, configurable: true });
}

beforeEach(() => {
  vi.mocked(runIdleQueue).mockClear();
  queued.tasks = [];
  dataMock.prefetch.mockReset();
  dataMock.has.mockReset().mockReturnValue(false);
  setConnection(undefined);
});

describe('startIdlePrefetch', () => {
  it('queues one code task per distinct section chunk, skipping the open section', () => {
    // transcription / asr-provider / custom-vocabulary share one lead chunk.
    startIdlePrefetch(
      ['profile', 'transcription', 'asr-provider', 'custom-vocabulary', 'download'],
      'user',
      'profile'
    );
    expect(queued.tasks).toHaveLength(2);
  });

  it('only queues what it was given: a section the user cannot open is never touched', () => {
    startIdlePrefetch(['profile'], 'user');
    expect(queued.tasks).toHaveLength(1);
    // no admin/super-admin row was passed, so none can have been warmed
    expect(dataMock.prefetch).not.toHaveBeenCalled();
  });

  it('adds a data task only for sections whose warm-up this role may run', () => {
    dataMock.has.mockImplementation((section: string) => section === 'processing-pipeline');
    startIdlePrefetch(['processing-pipeline', 'download'], 'user');
    // 2 code tasks + 1 data task
    expect(queued.tasks).toHaveLength(3);
    queued.tasks[2]();
    expect(dataMock.prefetch).toHaveBeenCalledWith('processing-pipeline', 'user');
    expect(dataMock.prefetch).toHaveBeenCalledTimes(1);
  });

  it('does nothing on Data Saver and returns a harmless cancel', () => {
    setConnection({ saveData: true });
    const cancel = startIdlePrefetch(['profile', 'download'], 'user');
    expect(runIdleQueue).not.toHaveBeenCalled();
    expect(() => cancel()).not.toThrow();
  });

  it('hands back the queue cancel so closing the modal stops the work', () => {
    const cancel = startIdlePrefetch(['profile'], 'user');
    cancel();
    expect(queued.cancel).toHaveBeenCalledTimes(1);
  });
});

describe('connectionAllowsPrefetch', () => {
  it('allows an unknown connection, blocks saveData and 2g', () => {
    expect(connectionAllowsPrefetch()).toBe(true);
    setConnection({ effectiveType: '4g' });
    expect(connectionAllowsPrefetch()).toBe(true);
    setConnection({ effectiveType: '2g' });
    expect(connectionAllowsPrefetch()).toBe(false);
    setConnection({ effectiveType: 'slow-2g' });
    expect(connectionAllowsPrefetch()).toBe(false);
    setConnection({ saveData: true, effectiveType: '4g' });
    expect(connectionAllowsPrefetch()).toBe(false);
  });
});
