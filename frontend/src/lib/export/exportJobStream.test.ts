import { describe, it, expect, vi, afterEach } from 'vitest';
import { openExportJobStream, ExportJobError } from './exportJobStream';

/** A hand-driven EventSource: the test fires the events the server would send. */
class FakeSource {
  static CLOSED = 2;
  readyState = 1;
  closed = false;
  handlers: Record<string, ((event: MessageEvent) => void)[]> = {};
  constructor(public url: string) {}
  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    (this.handlers[name] ??= []).push(handler);
  }
  close() {
    this.closed = true;
    this.readyState = FakeSource.CLOSED;
  }
  fire(name: string, data?: unknown) {
    for (const handler of this.handlers[name] ?? []) {
      handler({ data: data === undefined ? undefined : JSON.stringify(data) } as MessageEvent);
    }
  }
}

function open(jobId = 'a'.repeat(64)) {
  let source!: FakeSource;
  const stream = openExportJobStream(jobId, (id) => {
    source = new FakeSource(id);
    return source as unknown as EventSource;
  });
  return { stream, source };
}

afterEach(() => vi.unstubAllGlobals());

describe('openExportJobStream', () => {
  it('hands the job id to the factory', () => {
    const { source } = open('x y');
    expect(source.url).toBe('x y');
  });

  it('by default opens the signed-job stream for the id, URL-encoded', () => {
    const opened: string[] = [];
    vi.stubGlobal(
      'EventSource',
      class {
        static CLOSED = 2;
        readyState = 1;
        constructor(url: string) {
          opened.push(url);
        }
        addEventListener() {}
        close() {}
      }
    );
    openExportJobStream('x y');
    expect(opened).toEqual(['/api/files/bulk-export-stream?job=x%20y']);
  });

  it('resolves on ready with the presigned URL and the server file name, then closes', async () => {
    const { stream, source } = open();
    source.fire('ready', {
      url: 'https://s/x.pdf',
      filename: 'اجتماع.pdf',
      exported: 1,
      skipped: 0,
    });
    await expect(stream.result).resolves.toEqual({
      url: 'https://s/x.pdf',
      filename: 'اجتماع.pdf',
      exported: 1,
      skipped: 0,
    });
    expect(source.closed).toBe(true);
  });

  it.each(['redaction_pending', 'too_long', 'unavailable', 'fonts_unavailable', 'failed'])(
    'rejects with the worker reason %s',
    async (reason) => {
      const { stream, source } = open();
      source.fire('error', { code: 'export_failed', reason, message: 'fixed sentence' });
      const error = await stream.result.catch((e) => e);
      expect(error).toBeInstanceOf(ExportJobError);
      expect(error.reason).toBe(reason);
      expect(source.closed).toBe(true);
    }
  );

  it('never trusts a reason it does not know', async () => {
    const { stream, source } = open();
    source.fire('error', { code: 'export_failed', reason: '<script>', message: 'boom' });
    const error = await stream.result.catch((e) => e);
    expect(error.reason).toBeNull();
  });

  it('ignores a transient connection drop (an error event with no data on an open stream)', async () => {
    const { stream, source } = open();
    source.fire('error');
    expect(source.closed).toBe(false);
    source.fire('ready', { url: 'https://s/x.docx', filename: 'x.docx' });
    await expect(stream.result).resolves.toMatchObject({ url: 'https://s/x.docx' });
  });

  it('fails when the stream is closed for good', async () => {
    const { stream, source } = open();
    source.readyState = FakeSource.CLOSED;
    source.fire('error');
    await expect(stream.result).rejects.toBeInstanceOf(ExportJobError);
  });

  it('settles once: a late event after the first is ignored', async () => {
    const { stream, source } = open();
    source.fire('ready', { url: 'https://s/first' });
    source.fire('error', { reason: 'failed' });
    await expect(stream.result).resolves.toMatchObject({ url: 'https://s/first' });
  });
});
