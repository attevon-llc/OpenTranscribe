/**
 * Save a presigned object-storage URL through a transient anchor click.
 *
 * The URL already carries `Content-Disposition: attachment` (the worker presigned it with the
 * RFC 6266 file name), so the browser downloads it rather than navigating; `filename` is only
 * the `download` hint for the rare response without that header. Shared by the gallery's bulk
 * export and the single-transcript document export so both save files the same way.
 */
export function triggerAnchorDownload(href: string, filename?: string | null): void {
  const anchor = document.createElement('a');
  anchor.href = href;
  if (filename) anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
