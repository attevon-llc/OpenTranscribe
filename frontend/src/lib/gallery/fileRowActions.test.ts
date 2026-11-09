import { describe, it, expect, vi, beforeEach } from 'vitest';

const { goto, handleMultiSelect, toggleFileSelection, prefetchFileDetails } = vi.hoisted(() => ({
  goto: vi.fn(),
  handleMultiSelect: vi.fn(),
  toggleFileSelection: vi.fn(),
  prefetchFileDetails: vi.fn(),
}));

vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$app/paths', () => ({ resolve: (_p: string, a: { id: string }) => `/files/${a.id}` }));
vi.mock('$stores/gallery', () => ({ galleryStore: { handleMultiSelect, toggleFileSelection } }));
vi.mock('$lib/prefetch', () => ({ prefetchFileDetails }));

import {
  activateFileRow,
  handleCheckboxChange,
  handleCheckboxLabelClick,
  prefetchOnMouseDown,
  uploadDate,
} from './fileRowActions';
import type { MediaFile } from '$lib/types/media';

const file = { uuid: 'f1', status: 'completed' } as unknown as MediaFile;
const click = (init: Partial<MouseEvent> = {}) =>
  ({
    preventDefault: vi.fn(),
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    ...init,
  }) as unknown as MouseEvent;

describe('fileRowActions', () => {
  beforeEach(() => vi.clearAllMocks());

  it('navigates on a plain click and reports the target', () => {
    const e = click();
    expect(activateFileRow(file, e, false, null)).toBe('f1');
    expect(e.preventDefault).toHaveBeenCalled();
    expect(goto).toHaveBeenCalledWith('/files/f1');
  });

  it('ignores a second click while navigating', () => {
    expect(activateFileRow(file, click(), false, 'other')).toBe('other');
    expect(goto).not.toHaveBeenCalled();
  });

  it('toggles selection in select mode and multi-selects with a modifier', () => {
    expect(activateFileRow(file, click(), true, null)).toBeNull();
    expect(toggleFileSelection).toHaveBeenCalledWith('f1');
    activateFileRow(file, click({ shiftKey: true }), true, null);
    expect(handleMultiSelect).toHaveBeenCalledWith('f1', true);
    expect(goto).not.toHaveBeenCalled();
  });

  it('multi-selects on a modifier click outside select mode', () => {
    activateFileRow(file, click({ ctrlKey: true }), false, null);
    expect(handleMultiSelect).toHaveBeenCalledWith('f1', false);
    expect(goto).not.toHaveBeenCalled();
  });

  it('prefetches on mousedown only when not selecting', () => {
    prefetchOnMouseDown(file, false);
    prefetchOnMouseDown(file, true);
    expect(prefetchFileDetails).toHaveBeenCalledTimes(1);
    expect(prefetchFileDetails).toHaveBeenCalledWith('f1', 'completed');
  });

  it('checkbox handlers', () => {
    const ev = { stopPropagation: vi.fn(), preventDefault: vi.fn() } as unknown as Event;
    handleCheckboxChange('f1', ev);
    expect(toggleFileSelection).toHaveBeenCalledWith('f1');
    handleCheckboxLabelClick('f1', click());
    expect(handleMultiSelect).not.toHaveBeenCalled();
    handleCheckboxLabelClick('f1', click({ shiftKey: true }));
    expect(handleMultiSelect).toHaveBeenCalledWith('f1', true);
  });

  it('uploadDate falls back to the server string', () => {
    expect(uploadDate({ formatted_upload_date: 'Jan 1' } as MediaFile, 'en')).toBe('Jan 1');
    expect(uploadDate({} as MediaFile, 'en')).toBe('');
  });
});
