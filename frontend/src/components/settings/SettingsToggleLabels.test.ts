/**
 * Every settings switch that once sat inside a wrapping <label> must still toggle when its row
 * text is clicked, and must be reachable as a role=switch named by that text.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

const dirSync = vi.hoisted(() => ({
  getDirectorySyncSettings: vi.fn(),
  updateDirectorySyncSettings: vi.fn(),
  getDirectorySyncStatus: vi.fn(),
  runDirectorySyncNow: vi.fn(),
}));
vi.mock('$lib/api/directorySyncApi', () => dirSync);

const redaction = vi.hoisted(() => ({
  getRedactionSettings: vi.fn(),
  getRedactionDefaults: vi.fn(),
  updateRedactionSettings: vi.fn(),
  resetRedactionSettings: vi.fn(),
}));
vi.mock('$lib/api/redactionSettings', async (orig) => ({
  ...(await orig<typeof import('$lib/api/redactionSettings')>()),
  ...redaction,
}));

const backup = vi.hoisted(() => ({
  getBackupSettings: vi.fn(),
  updateBackupSettings: vi.fn(),
  getBackupStatus: vi.fn(),
  runBackupNow: vi.fn(),
  listBackups: vi.fn(),
  testS3Connection: vi.fn(),
}));
vi.mock('$lib/api/backupApi', async (orig) => ({
  ...(await orig<typeof import('$lib/api/backupApi')>()),
  ...backup,
}));

import DirectorySyncSettings from './DirectorySyncSettings.svelte';
import ContentRedactionSettings from './ContentRedactionSettings.svelte';
import BackupSettings from './BackupSettings.svelte';
import { DEFAULT_REDACTION_SETTINGS } from '$lib/api/redactionSettings';

beforeEach(() => vi.clearAllMocks());

async function flipByText(name: string) {
  const sw = (await screen.findByRole('switch', { name })) as HTMLInputElement;
  const before = sw.checked;
  await fireEvent.click(screen.getByText(name));
  await waitFor(() => expect(sw.checked).toBe(!before));
}

describe('settings switches stay clickable by their text', () => {
  it('directory sync: enable and dry run', async () => {
    const cfg = { enabled: false, schedule: '0 4 * * *', dry_run: true, max_disables_per_run: 10 };
    dirSync.getDirectorySyncSettings.mockResolvedValue(cfg);
    dirSync.getDirectorySyncStatus.mockResolvedValue({ ...cfg, next_due: false });
    render(DirectorySyncSettings);
    await flipByText('settings.directorySync.enableLabel');
    await flipByText('settings.directorySync.dryRunLabel');
  });

  it('content redaction: enabled and both privacy switches', async () => {
    redaction.getRedactionSettings.mockResolvedValue({ ...DEFAULT_REDACTION_SETTINGS });
    redaction.getRedactionDefaults.mockResolvedValue({
      available_detectors: [],
      available_categories: [],
      available_pii_entities: [],
      available_styles: [],
      locked_categories: [],
      export_locked: false,
      redact_before_llm_locked: false,
      profanity_languages: [],
      pii_languages: [],
      toxicity_languages: [],
    });
    render(ContentRedactionSettings);
    await flipByText('settings.contentRedaction.enabled');
    await flipByText('settings.contentRedaction.redactBeforeLlm');
    await flipByText('settings.contentRedaction.exportRedacted');
  });

  it('backup: enable, encrypt and OpenSearch snapshot', async () => {
    const cfg = {
      enabled: false,
      schedule: '0 3 * * *',
      destination: '/backups',
      retention_daily: 7,
      retention_weekly: 4,
      retention_monthly: 12,
      encrypt: false,
      passphrase_file: '',
      include_opensearch: false,
      destination_type: 'local',
      s3_endpoint_url: '',
      s3_region: '',
      s3_bucket: '',
      s3_prefix: '',
      s3_access_key_id: '',
      s3_secret_key_set: false,
      destination_status: { path: '/backups', exists: true, writable: true },
    };
    backup.getBackupSettings.mockResolvedValue(cfg);
    backup.getBackupStatus.mockResolvedValue({
      ...cfg,
      next_due: false,
      pg_dump_available: true,
    });
    backup.listBackups.mockResolvedValue({ backups: [] });
    render(BackupSettings);
    await flipByText('settings.backup.enableLabel');
    await flipByText('settings.backup.encryptLabel');
    await flipByText('settings.backup.includeOpensearchLabel');
  });
});
