import { describe, it, expect, vi, beforeEach } from 'vitest';

const post = vi.fn();
vi.mock('$lib/axios', () => ({ default: { post: (...args: unknown[]) => post(...args) } }));

import {
  documentTemplateLabels,
  requestDocumentExport,
  startDocumentExport,
  type DocumentExportRequest,
} from './requestDocumentExport';
import { ExportJobError } from './exportJobStream';

class FakeSource {
  static CLOSED = 2;
  readyState = 1;
  handlers: Record<string, ((event: MessageEvent) => void)[]> = {};
  constructor(public jobId: string) {}
  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    (this.handlers[name] ??= []).push(handler);
  }
  close() {
    this.readyState = FakeSource.CLOSED;
  }
  fire(name: string, data: unknown) {
    for (const h of this.handlers[name] ?? []) h({ data: JSON.stringify(data) } as MessageEvent);
  }
}

const LABELS = {
  speaker_default_label: 'Speaker',
  user_comment_label: 'USER COMMENT',
  page_label: 'Page {page} of {pages}',
  exported_label: 'Exported {date}',
  redacted_label: 'Redacted',
};

const REQUEST: DocumentExportRequest = {
  fileUuid: 'file-uuid',
  format: 'pdf',
  includeComments: false,
  includeTimestamps: true,
  includeSpeakers: true,
  lineNumbering: 'per_page',
  pageSize: 'a4',
  labels: LABELS,
};

let source: FakeSource;
const factory = (jobId: string) => {
  source = new FakeSource(jobId);
  return source as unknown as EventSource;
};

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({ data: { status: 'processing', job_id: 'j'.repeat(64) } });
});

describe('requestDocumentExport (issue #1278)', () => {
  it('POSTs the options to the document route, never GETs a serialized document', async () => {
    await startDocumentExport(REQUEST, factory);
    expect(post).toHaveBeenCalledTimes(1);
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe('/files/file-uuid/export/document');
    expect(body).toMatchObject({
      format: 'pdf',
      include_comments: false,
      include_timestamps: true,
      include_speakers: true,
      line_numbering: 'per_page',
      page_size: 'a4',
      labels: LABELS,
    });
  });

  it('omits reveal_original unless the reader is viewing the original', async () => {
    await startDocumentExport(REQUEST, factory);
    expect('reveal_original' in (post.mock.calls[0][1] as object)).toBe(false);
    await startDocumentExport({ ...REQUEST, showOriginal: true }, factory);
    expect((post.mock.calls[1][1] as Record<string, unknown>).reveal_original).toBe(true);
  });

  it('listens on the stream for the job id the server issued', async () => {
    await startDocumentExport(REQUEST, factory);
    expect(source.jobId).toBe('j'.repeat(64));
  });

  it('resolves with the presigned URL when the job completes', async () => {
    const pending = requestDocumentExport(REQUEST, factory);
    await vi.waitFor(() => expect(source).toBeDefined());
    source.fire('ready', { url: 'https://s/doc.pdf', filename: 'doc.pdf', exported: 1 });
    await expect(pending).resolves.toMatchObject({ url: 'https://s/doc.pdf', filename: 'doc.pdf' });
  });

  it('rejects with the job reason and builds no Blob when the job fails', async () => {
    const blob = vi.spyOn(globalThis, 'Blob');
    const pending = requestDocumentExport(REQUEST, factory);
    await vi.waitFor(() => expect(source).toBeDefined());
    source.fire('error', { code: 'export_failed', reason: 'redaction_pending', message: 'x' });
    const error = await pending.catch((e) => e);
    expect(error).toBeInstanceOf(ExportJobError);
    expect(error.reason).toBe('redaction_pending');
    expect(blob).not.toHaveBeenCalled();
    blob.mockRestore();
  });

  it('lets a refusal at dispatch (409, 503) reach the caller with no local fallback', async () => {
    post.mockRejectedValue({ response: { status: 409 } });
    const blob = vi.spyOn(globalThis, 'Blob');
    await expect(requestDocumentExport(REQUEST, factory)).rejects.toMatchObject({
      response: { status: 409 },
    });
    expect(blob).not.toHaveBeenCalled();
    blob.mockRestore();
  });
});

describe('documentTemplateLabels', () => {
  it('hands the server its placeholders literally', () => {
    // i18next fills {{page}} from the params; passing the placeholder text back as the value is
    // what leaves a literal {page} for the server to substitute.
    const translate = (key: string, params?: Record<string, string>) =>
      ({
        'documentExport.pageLabel': 'Page {{page}} of {{pages}}',
        'documentExport.exportedLabel': 'Exported {{date}}',
      })[key]!.replace(/{{(\w+)}}/g, (_, name: string) => params?.[name] ?? '');
    expect(documentTemplateLabels(translate)).toEqual({
      page_label: 'Page {page} of {pages}',
      exported_label: 'Exported {date}',
    });
  });
});
