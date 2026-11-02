/**
 * Query-string deep links into the file-detail page.
 */

/**
 * `?view=summary[&section=N]` — the deep link a `kind: "summary"` chat citation points at
 * (issue #464 amendment c). A summary citation labels machine-generated prose about the
 * whole recording, never a moment in it, so it opens the summary modal instead of seeking.
 * `section` is accepted (forward-compatible with the citation's `digest_section`) but not
 * yet used: `summary_data` has no per-section structure to scroll to or highlight.
 */
export function opensSummaryView(search: string): boolean {
  return new URLSearchParams(search).get('view') === 'summary';
}
