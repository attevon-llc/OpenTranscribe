import { describe, expect, it } from 'vitest';
import type { WatchSource } from '$lib/api/watchSourcesApi';
import {
  DEFAULT_MULTIPART_REGEX,
  blankWatchSourceForm,
  buildWatchSourcePayload,
  formFromSource,
} from './watchSourceForm';

const blank = () => blankWatchSourceForm('local');

const source = (over: Partial<WatchSource> = {}) =>
  ({
    uuid: 's1',
    name: 'Inbox',
    source_type: 's3',
    is_enabled: true,
    local_path: null,
    delete_after_import: false,
    s3_endpoint_url: null,
    s3_bucket_name: 'bkt',
    s3_prefix: 'in/',
    s3_region: null,
    s3_access_key_id: 'AKIA',
    s3_use_ssl: true,
    smb_server: null,
    smb_share: null,
    smb_path: null,
    smb_username: null,
    smb_domain: null,
    smb_port: 445,
    polling_interval_minutes: 5,
    use_fs_events: false,
    file_extensions: null,
    skip_files_older_than_days: null,
    recursive: true,
    auto_transcribe: true,
    min_speakers: null,
    max_speakers: 4,
    multipart_enabled: true,
    multipart_regex: '',
    multipart_time_window_hours: 12,
    multipart_wait_scans: 2,
    upload_stitched_to_source: false,
    tag_names: [],
    ...over,
  }) as unknown as WatchSource;

describe('formFromSource', () => {
  it('turns nulls into blanks, blanks every secret, and falls back to the default regex', () => {
    const form = formFromSource(source(), blank());
    expect(form).toMatchObject({
      name: 'Inbox',
      source_type: 's3',
      s3_secret_key: '',
      smb_password: '',
      smb_path: '/',
      file_extensions: '',
      skip_files_older_than_days: null,
      max_speakers: 4,
      multipart_regex: DEFAULT_MULTIPART_REGEX,
    });
  });
});

describe('buildWatchSourcePayload', () => {
  it('sends the S3 fields only, trimmed, and the secret only when typed', () => {
    const form = {
      ...blank(),
      name: ' Inbox ',
      source_type: 's3' as const,
      s3_bucket_name: ' bkt ',
      s3_access_key_id: ' AKIA ',
      s3_prefix: ' ',
    };
    const payload = buildWatchSourcePayload(form, ['t'], ['c1']);
    expect(payload).toMatchObject({
      name: 'Inbox',
      s3_bucket_name: 'bkt',
      s3_access_key_id: 'AKIA',
      s3_prefix: null,
      tag_names: ['t'],
      collection_ids: ['c1'],
    });
    expect(payload).not.toHaveProperty('s3_secret_key');
    expect(payload).not.toHaveProperty('smb_server');
    expect(payload).not.toHaveProperty('local_path');
    expect(buildWatchSourcePayload({ ...form, s3_secret_key: ' k ' }, [], [])).toHaveProperty(
      's3_secret_key',
      'k'
    );
  });

  it('sends the local path and delete flag for a local source', () => {
    const payload = buildWatchSourcePayload(
      { ...blank(), local_path: ' /data ', delete_after_import: true },
      [],
      []
    );
    expect(payload).toMatchObject({ local_path: '/data', delete_after_import: true });
  });

  it('sends the SMB fields with a numeric port, root path default and typed password only', () => {
    const payload = buildWatchSourcePayload(
      {
        ...blank(),
        source_type: 'smb',
        smb_server: ' h ',
        smb_share: ' s ',
        smb_path: '',
        smb_port: 445,
      },
      [],
      []
    );
    expect(payload).toMatchObject({
      smb_server: 'h',
      smb_share: 's',
      smb_path: '/',
      smb_port: 445,
    });
    expect(payload).not.toHaveProperty('smb_password');
  });

  it('reads an emptied number input as null and a blank speaker range as the saved range', () => {
    const payload = buildWatchSourcePayload(
      { ...blank(), skip_files_older_than_days: '', min_speakers: null, max_speakers: 3 },
      [],
      []
    );
    expect(payload).toMatchObject({
      skip_files_older_than_days: null,
      min_speakers: null,
      max_speakers: 3,
    });
    expect(
      buildWatchSourcePayload({ ...blank(), skip_files_older_than_days: 0 }, [], [])
    ).toMatchObject({ skip_files_older_than_days: 0 });
  });
});
