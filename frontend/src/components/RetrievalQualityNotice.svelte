<!--
  RetrievalQualityNotice.svelte — an honest note about retrieval quality (#461).

  This is deliberately NOT a generic "beta" badge. Retrieval quality here is
  measured continuously, and issue #461 records one open finding with direct
  product impact: the cross-encoder reranking step appears to DEGRADE retrieval
  ordering (−20.6% nDCG@10 on QMSum, −32.7% on the synthetic corpus), while
  whether it helps or hurts the final ANSWER has not been measured at all. The
  link is the point — it turns a disclaimer into an invitation to read the
  numbers.

  Two surfaces, two different true statements, hence the `surface` prop:

  - `chat`   — the reranker's only production call site is the chat retrieval
               path (`services/chat/retrieval.py`), and it is on by default.
  - `search` — `/search` is ranked by OpenSearch RRF fusion and never touches
               the reranker. #461 §2 measured 24 fusion arms and adopted none,
               and §3 showed the two eval corpora are anti-correlated. So the
               ranking is measured-but-unimproved, which is worth saying; the
               reranker claim would be false here.

  Blurring the two into one sentence would look fine on screen and be wrong, so
  `RetrievalQualityNotice.test.ts` pins that they differ.

  Nothing here says anything is broken: this is about accuracy of results, not
  stability.
-->
<script lang="ts">
  import { t } from '$stores/locale';

  /** Which surface this is rendered on — selects the copy and the dismissal key. */
  export let surface: 'chat' | 'search' = 'chat';

  /** Where the measurements actually live. */
  const ISSUE_URL = 'https://github.com/attevon-llc/OpenTranscribe/issues/461';

  /**
   * Dismissal is per-surface: the two notices report different facts, so having
   * read one is not a reason to hide the other. It is a UI preference, which is
   * why `clearUserState` deliberately leaves it alone on logout.
   */
  $: storageKey = `opentr:retrievalQualityNotice:${surface}`;

  function readDismissed(key: string): boolean {
    try {
      return localStorage.getItem(key) === 'dismissed';
    } catch {
      // Private browsing / disabled storage — show the notice rather than swallow it.
      return false;
    }
  }

  $: dismissed = readDismissed(storageKey);

  function dismiss(): void {
    try {
      localStorage.setItem(storageKey, 'dismissed');
    } catch {
      // Unwritable storage still gets a dismissal for this session.
    }
    dismissed = true;
  }
</script>

{#if !dismissed}
  <div class="quality-notice" role="note" data-testid="retrieval-quality-notice">
    <svg
      class="notice-icon"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>

    <p class="notice-text">
      {surface === 'search'
        ? $t('retrievalQuality.searchMessage')
        : $t('retrievalQuality.chatMessage')}
      <a
        class="notice-link"
        href={ISSUE_URL}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="retrieval-quality-link"
      >
        {$t('retrievalQuality.learnMore')}
      </a>
    </p>

    <button
      type="button"
      class="notice-dismiss"
      on:click={dismiss}
      aria-label={$t('retrievalQuality.dismiss')}
      title={$t('retrievalQuality.dismiss')}
      data-testid="retrieval-quality-dismiss"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2.5"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    </button>
  </div>
{/if}

<style>
  /* Neutral, not alarmed: nothing is broken, so no warning colour. Every value
     is a theme token, so light/dark parity comes for free. */
  .quality-notice {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    width: 100%;
    padding: 0.55rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-secondary, var(--background-secondary));
    color: var(--text-secondary);
    font-size: 0.8rem;
    line-height: 1.5;
    text-align: left;
  }

  .notice-icon {
    flex: 0 0 auto;
    margin-top: 0.15rem;
    color: var(--text-secondary);
  }

  .notice-text {
    margin: 0;
    min-width: 0;
    flex: 1 1 auto;
  }

  .notice-link {
    color: var(--primary-on-surface);
    text-decoration: underline;
    text-underline-offset: 2px;
    white-space: nowrap;
  }

  .notice-link:hover {
    color: var(--primary-hover);
  }

  .notice-link:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }

  .notice-dismiss {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.2rem;
    border: none;
    border-radius: 4px;
    background: none;
    color: var(--text-secondary);
    cursor: pointer;
    transition: background-color 0.15s ease, color 0.15s ease;
  }

  .notice-dismiss:hover {
    background-color: var(--button-hover, var(--surface-hover));
    color: var(--text-color);
  }

  .notice-dismiss:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
</style>
