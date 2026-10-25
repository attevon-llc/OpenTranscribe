/**
 * The one consumer of the signed-job stream (`GET /api/files/bulk-export-stream?job=`).
 *
 * A job id is minted by an endpoint that queued server-side work (the gallery's bulk subtitle
 * ZIP, a single transcript's DOCX/PDF) and is HMAC-bound to the signed-in user; the stream
 * delivers the presigned download URL, or the reason the job failed. It used to be inlined in
 * the gallery page; two consumers of one protocol drift, so both use this.
 *
 * Event contract (`api/endpoints/files/subtitles.py`): `ready` carries
 * `{url, filename, exported, skipped}`; `error` carries `{code: 'export_failed', message}` and,
 * for a document export, a `reason` from {@link DocumentExportReason}; `progress` is ignored.
 *
 * ⚠️ The browser fires `error` both for a server `event: error` (has `data`) and for a dropped
 * connection (no `data`). Only the first, or a CLOSED stream, is a failure: a transient drop is
 * retried by EventSource itself.
 */

/** Why a document export ended in `error`, as sent by the worker (a fixed set of slugs). */
export type DocumentExportReason =
  | 'redaction_pending'
  | 'too_long'
  | 'unavailable'
  | 'fonts_unavailable'
  | 'failed';

/** `EventSource.CLOSED`, inlined: the constant is a global that not every runtime defines. */
const CLOSED = 2;

const KNOWN_REASONS: readonly string[] = [
  'redaction_pending',
  'too_long',
  'unavailable',
  'fonts_unavailable',
  'failed',
];

export interface ExportJobResult {
  url: string;
  filename: string | null;
  exported: number;
  skipped: number;
}

export class ExportJobError extends Error {
  /** The worker's reason, or `null` for an older worker or a closed connection. */
  readonly reason: DocumentExportReason | null;

  constructor(reason: DocumentExportReason | null, message: string) {
    super(message);
    this.name = 'ExportJobError';
    this.reason = reason;
  }
}

export interface ExportJobStream {
  /** Settles on the first `ready` or terminal `error`; the stream is closed either way. */
  result: Promise<ExportJobResult>;
  /** Stop listening (a newer job replaced this one). Leaves `result` pending. */
  close: () => void;
}

export type EventSourceFactory = (url: string) => EventSource;

const defaultFactory: EventSourceFactory = (url) => new EventSource(url);

function parse(data: unknown): Record<string, unknown> {
  try {
    const value = JSON.parse(String(data));
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export function openExportJobStream(
  jobId: string,
  factory: EventSourceFactory = defaultFactory
): ExportJobStream {
  const source = factory(`/api/files/bulk-export-stream?job=${encodeURIComponent(jobId)}`);
  let settled = false;

  const result = new Promise<ExportJobResult>((resolve, reject) => {
    const finish = (settle: () => void) => {
      if (settled) return;
      settled = true;
      source.close();
      settle();
    };

    source.addEventListener('ready', (event) => {
      const data = parse((event as MessageEvent).data);
      finish(() =>
        resolve({
          url: String(data.url ?? ''),
          filename: typeof data.filename === 'string' ? data.filename : null,
          exported: Number(data.exported ?? 0),
          skipped: Number(data.skipped ?? 0),
        })
      );
    });

    source.addEventListener('error', (event) => {
      const raw = (event as MessageEvent).data;
      if (!raw && source.readyState !== CLOSED) return; // transient drop: retried
      const data = parse(raw);
      const reason =
        typeof data.reason === 'string' && KNOWN_REASONS.includes(data.reason)
          ? (data.reason as DocumentExportReason)
          : null;
      finish(() => reject(new ExportJobError(reason, String(data.message ?? 'Export failed.'))));
    });
  });

  return {
    result,
    close: () => {
      settled = true;
      source.close();
    },
  };
}
