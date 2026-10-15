import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  rowView,
  errorKeyFor,
  apiErrorCode,
  formatBytes,
  isDownloadActive,
  LOCAL_MODEL_ERROR_CODES,
} from './localModelView';
import type { LocalModelDownloadState, LocalModelEntry } from '$lib/api/localModels';

const entry = (over: Partial<LocalModelEntry> = {}): LocalModelEntry => ({
  id: 'small',
  display_name: 'small',
  repo_id: 'Systran/faster-whisper-small',
  approx_bytes: 485_000_000,
  vram_hint_gb: 2,
  language_support: 'multilingual',
  supports_translation: true,
  is_default: false,
  downloaded: false,
  partial: false,
  present_bytes: 0,
  is_active: false,
  ...over,
});

const dl = (over: Partial<LocalModelDownloadState> = {}): LocalModelDownloadState => ({
  model_id: 'small',
  status: 'downloading',
  bytes_done: 100,
  bytes_total: 400,
  percent: 25,
  error_code: null,
  updated_at: '2026-10-15T00:00:00Z',
  ...over,
});

describe('rowView', () => {
  it('offers Download and a disabled Set active for a model that is not on disk', () => {
    const v = rowView(entry(), null);
    expect(v.status).toBe('notDownloaded');
    expect(v.showDownload).toBe(true);
    expect(v.downloadDisabled).toBe(false);
    expect(v.showActivate).toBe(true);
    expect(v.activateDisabled).toBe(true);
    expect(v.showCancel).toBe(false);
  });

  it('calls an interrupted download partial and still offers Download', () => {
    const v = rowView(entry({ partial: true }), null);
    expect(v.status).toBe('partial');
    expect(v.showDownload).toBe(true);
    expect(v.activateDisabled).toBe(true);
  });

  it('enables Set active for a downloaded model', () => {
    const v = rowView(entry({ downloaded: true }), null);
    expect(v.status).toBe('downloaded');
    expect(v.showDownload).toBe(false);
    expect(v.activateDisabled).toBe(false);
  });

  it('marks the active model and offers nothing to activate', () => {
    const v = rowView(entry({ downloaded: true, is_active: true }), null);
    expect(v.status).toBe('active');
    expect(v.showActivate).toBe(false);
  });

  it('shows progress and Cancel on the model that is downloading', () => {
    const v = rowView(entry(), dl());
    expect(v.status).toBe('downloading');
    expect(v.percent).toBe(25);
    expect(v.showCancel).toBe(true);
    expect(v.showDownload).toBe(false);
    expect(v.activateDisabled).toBe(true);
  });

  it.each(['queued', 'verifying'] as const)('treats %s as in flight', (status) => {
    const v = rowView(entry(), dl({ status, percent: null }));
    expect(v.status).toBe(status);
    expect(v.showCancel).toBe(true);
    expect(v.percent).toBeNull();
  });

  it('disables Download on every other row while one download runs', () => {
    const v = rowView(entry({ id: 'base' }), dl());
    expect(v.showDownload).toBe(true);
    expect(v.downloadDisabled).toBe(true);
    expect(v.showCancel).toBe(false);
  });

  it('lets a downloaded model be activated while another one downloads', () => {
    expect(rowView(entry({ id: 'base', downloaded: true }), dl()).activateDisabled).toBe(false);
  });

  it('shows a failed download as a fixed sentence key and lets the user retry', () => {
    const v = rowView(entry(), dl({ status: 'failed', error_code: 'hf_unreachable' }));
    expect(v.errorKey).toBe('settings.localModels.error.hf_unreachable');
    expect(v.showDownload).toBe(true);
    expect(v.downloadDisabled).toBe(false);
  });

  it('does not show a stale failure on a different model', () => {
    const v = rowView(entry({ id: 'base' }), dl({ status: 'failed', error_code: 'disk_full' }));
    expect(v.errorKey).toBeNull();
  });

  it('shows no error for a cancelled or completed download', () => {
    expect(rowView(entry(), dl({ status: 'cancelled' })).errorKey).toBeNull();
    expect(rowView(entry({ downloaded: true }), dl({ status: 'completed' })).errorKey).toBeNull();
  });

  it('disables Download when the cache is not writable', () => {
    expect(rowView(entry(), null, { cacheWritable: false }).downloadDisabled).toBe(true);
  });
});

describe('isDownloadActive', () => {
  it.each([
    ['queued', true],
    ['downloading', true],
    ['verifying', true],
    ['completed', false],
    ['failed', false],
    ['cancelled', false],
  ] as const)('%s -> %s', (status, expected) => {
    expect(isDownloadActive(dl({ status }))).toBe(expected);
  });

  it('is false with no download', () => {
    expect(isDownloadActive(null)).toBe(false);
  });
});

describe('errorKeyFor', () => {
  const en = JSON.parse(
    readFileSync(resolve(__dirname, '../i18n/locales/en.json'), 'utf8')
  ) as Record<string, string>;
  const sentenceFor = (code: string) => en[`settings.localModels.error.${code}`];

  it('has a sentence in en.json for every code the backend can send', () => {
    for (const code of LOCAL_MODEL_ERROR_CODES) {
      expect(sentenceFor(code), code).toEqual(expect.any(String));
      expect(errorKeyFor(code)).toBe(`settings.localModels.error.${code}`);
    }
  });

  it('lists the failed-state codes of the downloader', () => {
    for (const code of [
      'hf_auth_failed',
      'hf_repo_not_found',
      'hf_unreachable',
      'disk_full',
      'cache_not_writable',
      'downloads_disabled_offline',
      'download_timed_out',
      'download_failed',
      'insufficient_disk_space',
      'model_download_in_progress',
      'unknown_local_model',
      'local_model_not_downloaded',
      'local_model_already_downloaded',
      'no_model_download',
      'model_download_unavailable',
      'gpu_worker_not_found',
      'local_asr_unavailable',
    ]) {
      expect(LOCAL_MODEL_ERROR_CODES).toContain(code);
    }
  });

  it('falls back to the generic failure for an unknown or missing code', () => {
    expect(errorKeyFor('something_new')).toBe('settings.localModels.error.download_failed');
    expect(errorKeyFor(null)).toBe('settings.localModels.error.download_failed');
  });
});

describe('apiErrorCode', () => {
  it('reads detail.code from an axios-style error', () => {
    const err = { response: { data: { detail: { code: 'insufficient_disk_space' } } } };
    expect(apiErrorCode(err)).toBe('insufficient_disk_space');
  });

  it('is null for a string detail, no response or a non-object', () => {
    expect(apiErrorCode({ response: { data: { detail: 'nope' } } })).toBeNull();
    expect(apiErrorCode(new Error('x'))).toBeNull();
    expect(apiErrorCode(undefined)).toBeNull();
  });
});

describe('formatBytes', () => {
  it('uses megabytes below a gigabyte and gigabytes above', () => {
    expect(formatBytes(485_000_000, 'en')).toBe('485 MB');
    expect(formatBytes(3_100_000_000, 'en')).toBe('3.1 GB');
  });

  it('formats with the active locale', () => {
    expect(formatBytes(3_100_000_000, 'de')).toMatch(/^3,1\s?GB$/);
  });
});
