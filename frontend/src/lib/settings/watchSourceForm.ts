/**
 * The watch-source modal's form model, the form an existing source opens with, and the request
 * body a save sends. A secret field starts blank when editing and is sent only when typed, so
 * the server keeps the stored secret otherwise.
 */
import type { SourceType, WatchSource } from '$lib/api/watchSourcesApi';

export const DEFAULT_MULTIPART_REGEX = '^(.+?)_P(\\d{3})(\\.[^.]+)$';

export interface WatchSourceForm {
  name: string;
  source_type: SourceType;
  is_enabled: boolean;
  local_path: string;
  delete_after_import: boolean;
  s3_endpoint_url: string;
  s3_bucket_name: string;
  s3_prefix: string;
  s3_region: string;
  s3_access_key_id: string;
  s3_secret_key: string;
  s3_use_ssl: boolean;
  smb_server: string;
  smb_share: string;
  smb_path: string;
  smb_username: string;
  smb_password: string;
  smb_domain: string;
  smb_port: number;
  polling_interval_minutes: number;
  use_fs_events: boolean;
  file_extensions: string;
  // An emptied number input binds as null or ''.
  skip_files_older_than_days: number | null | '';
  recursive: boolean;
  auto_transcribe: boolean;
  min_speakers: number | null;
  max_speakers: number | null;
  multipart_enabled: boolean;
  multipart_regex: string;
  multipart_time_window_hours: number;
  multipart_wait_scans: number;
  upload_stitched_to_source: boolean;
}

function s3Fields(src: WatchSource) {
  return {
    s3_endpoint_url: src.s3_endpoint_url ?? '',
    s3_bucket_name: src.s3_bucket_name ?? '',
    s3_prefix: src.s3_prefix ?? '',
    s3_region: src.s3_region ?? '',
    s3_access_key_id: src.s3_access_key_id ?? '',
    s3_secret_key: '',
    s3_use_ssl: src.s3_use_ssl,
  };
}

function smbFields(src: WatchSource) {
  return {
    smb_server: src.smb_server ?? '',
    smb_share: src.smb_share ?? '',
    smb_path: src.smb_path ?? '/',
    smb_username: src.smb_username ?? '',
    smb_password: '',
    smb_domain: src.smb_domain ?? '',
    smb_port: src.smb_port,
  };
}

function scanFields(src: WatchSource) {
  return {
    polling_interval_minutes: src.polling_interval_minutes,
    use_fs_events: src.use_fs_events,
    file_extensions: src.file_extensions ?? '',
    skip_files_older_than_days: src.skip_files_older_than_days ?? null,
    recursive: src.recursive,
    auto_transcribe: src.auto_transcribe,
    min_speakers: src.min_speakers ?? null,
    max_speakers: src.max_speakers ?? null,
  };
}

/** The form a new source opens with; the caller picks the initial type (local needs a mount). */
export function blankWatchSourceForm(sourceType: SourceType): WatchSourceForm {
  return {
    name: '',
    source_type: sourceType,
    is_enabled: true,
    local_path: '',
    delete_after_import: false,
    s3_endpoint_url: '',
    s3_bucket_name: '',
    s3_prefix: '',
    s3_region: '',
    s3_access_key_id: '',
    s3_secret_key: '',
    s3_use_ssl: true,
    smb_server: '',
    smb_share: '',
    smb_path: '/',
    smb_username: '',
    smb_password: '',
    smb_domain: '',
    smb_port: 445,
    polling_interval_minutes: 15,
    use_fs_events: false,
    file_extensions: '',
    skip_files_older_than_days: 30,
    recursive: true,
    auto_transcribe: true,
    // Blank = use the owner's saved speaker range (issue #1198).
    min_speakers: null,
    max_speakers: null,
    multipart_enabled: false,
    multipart_regex: DEFAULT_MULTIPART_REGEX,
    multipart_time_window_hours: 24,
    multipart_wait_scans: 3,
    upload_stitched_to_source: false,
  };
}

function multipartFields(src: WatchSource) {
  return {
    multipart_enabled: src.multipart_enabled,
    multipart_regex: src.multipart_regex || DEFAULT_MULTIPART_REGEX,
    multipart_time_window_hours: src.multipart_time_window_hours,
    multipart_wait_scans: src.multipart_wait_scans,
    upload_stitched_to_source: src.upload_stitched_to_source,
  };
}

/** The form an existing source opens with; `base` supplies any field the source does not. */
export function formFromSource(src: WatchSource, base: WatchSourceForm): WatchSourceForm {
  return {
    ...base,
    name: src.name,
    source_type: src.source_type,
    is_enabled: src.is_enabled,
    local_path: src.local_path ?? '',
    delete_after_import: src.delete_after_import,
    ...s3Fields(src),
    ...smbFields(src),
    ...scanFields(src),
    ...multipartFields(src),
  };
}

const trimmedOrNull = (value: string | undefined): string | null => value?.trim() || null;

// A blank speaker field is sent as null, which the server reads as "my saved range".
function optionalCount(value: unknown): number | null {
  return value === null || value === undefined || value === '' ? null : Number(value);
}

function secretIfTyped<K extends string>(key: K, value: string | undefined) {
  const trimmed = value?.trim();
  return trimmed ? ({ [key]: trimmed } as Record<K, string>) : {};
}

function localPayload(form: WatchSourceForm) {
  return {
    local_path: form.local_path?.trim() || '',
    delete_after_import: form.delete_after_import,
  };
}

function s3Payload(form: WatchSourceForm) {
  return {
    s3_endpoint_url: trimmedOrNull(form.s3_endpoint_url),
    s3_bucket_name: form.s3_bucket_name.trim(),
    s3_prefix: trimmedOrNull(form.s3_prefix),
    s3_region: trimmedOrNull(form.s3_region),
    s3_access_key_id: form.s3_access_key_id.trim(),
    s3_use_ssl: form.s3_use_ssl,
    ...secretIfTyped('s3_secret_key', form.s3_secret_key),
  };
}

function smbPayload(form: WatchSourceForm) {
  return {
    smb_server: form.smb_server.trim(),
    smb_share: form.smb_share.trim(),
    smb_path: form.smb_path?.trim() || '/',
    smb_username: trimmedOrNull(form.smb_username),
    smb_domain: trimmedOrNull(form.smb_domain),
    smb_port: Number(form.smb_port),
    ...secretIfTyped('smb_password', form.smb_password),
  };
}

const SOURCE_PAYLOADS: Record<SourceType, (form: WatchSourceForm) => Record<string, unknown>> = {
  local: localPayload,
  s3: s3Payload,
  smb: smbPayload,
};

/** The request body for saving `form`; the source-type fields are those of the chosen type. */
export function buildWatchSourcePayload(
  form: WatchSourceForm,
  tagNames: string[],
  collectionIds: string[]
): Record<string, unknown> {
  const skipDays = form.skip_files_older_than_days;
  return {
    name: form.name.trim(),
    is_enabled: form.is_enabled,
    polling_interval_minutes: Number(form.polling_interval_minutes),
    use_fs_events: form.use_fs_events,
    file_extensions: trimmedOrNull(form.file_extensions),
    skip_files_older_than_days: skipDays === null || skipDays === '' ? null : Number(skipDays),
    recursive: form.recursive,
    auto_transcribe: form.auto_transcribe,
    min_speakers: optionalCount(form.min_speakers),
    max_speakers: optionalCount(form.max_speakers),
    tag_names: tagNames,
    collection_ids: collectionIds,
    multipart_enabled: form.multipart_enabled,
    multipart_regex: form.multipart_regex,
    multipart_time_window_hours: Number(form.multipart_time_window_hours),
    multipart_wait_scans: Number(form.multipart_wait_scans),
    upload_stitched_to_source: form.upload_stitched_to_source,
    ...SOURCE_PAYLOADS[form.source_type]?.(form),
  };
}
