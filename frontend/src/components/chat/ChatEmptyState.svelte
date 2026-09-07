<!--
  ChatEmptyState.svelte — the new-conversation hero.

  Two distinct empty states, because they need different responses from the user:
  no LLM configured is a setup problem (deep-link them to Settings), while an
  empty new chat just needs a nudge with example questions.

  The suggestions are written for transcript work specifically — action items,
  decisions, follow-ups — so a first-time user immediately sees what the feature
  is FOR rather than facing a blank box.

  The hero subtitle makes an accuracy claim ("cite the exact moment they came
  from"), so the retrieval-quality notice (#461) is amended onto it right here
  rather than banner-ing every conversation. This is also the ONE place in chat
  where such a notice renders exactly once: `ChatSources` was the other
  candidate, but it renders per assistant message, so expanding three source
  lists would put three copies of the same sentence on screen.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import RetrievalQualityNotice from '$components/RetrievalQualityNotice.svelte';

  /** LLM availability — false shows the setup CTA instead of suggestions. */
  export let llmAvailable = true;

  const dispatch = createEventDispatcher<{ suggestion: string }>();

  $: suggestions = [
    $t('chat.empty.suggestion1'),
    $t('chat.empty.suggestion2'),
    $t('chat.empty.suggestion3'),
  ];

  function openLlmSettings(): void {
    settingsModalStore.open('llm-provider');
  }
</script>

<div class="empty-state" data-testid="chat-empty-state">
  {#if llmAvailable}
    <div class="hero">
      <div class="hero-icon" aria-hidden="true">
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.75"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
        </svg>
      </div>
      <h1>{$t('chat.empty.heroTitle')}</h1>
      <p>{$t('chat.empty.heroSubtitle')}</p>
      <RetrievalQualityNotice surface="chat" />
    </div>

    <div class="suggestions">
      {#each suggestions as suggestion}
        <button
          type="button"
          class="suggestion-card"
          on:click={() => dispatch('suggestion', suggestion)}
          data-testid="chat-suggestion"
        >
          {suggestion}
        </button>
      {/each}
    </div>
  {:else}
    <div class="hero">
      <div class="hero-icon warning" aria-hidden="true">
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.75"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      </div>
      <h1>{$t('chat.setup.noLlmTitle')}</h1>
      <p>{$t('chat.setup.noLlmMessage')}</p>
      <button
        type="button"
        class="btn btn-primary"
        on:click={openLlmSettings}
        data-testid="chat-open-llm-settings"
      >
        {$t('chat.setup.openSettings')}
      </button>
    </div>
  {/if}
</div>

<style>
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2rem;
    flex: 1;
    padding: 2rem 1rem;
    text-align: center;
  }

  .hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    max-width: 34rem;
  }

  .hero-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background-color: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-on-surface);
  }

  .hero-icon.warning {
    background-color: rgba(var(--warning-color-rgb, 255, 193, 7), 0.15);
    color: var(--warning-color, #ffc107);
  }

  h1 {
    margin: 0;
    font-size: 1.4rem;
    font-weight: 600;
    color: var(--text-color);
  }

  p {
    margin: 0;
    font-size: 0.92rem;
    line-height: 1.6;
    color: var(--text-secondary);
  }

  .suggestions {
    display: grid;
    gap: 0.6rem;
    width: 100%;
    max-width: 34rem;
    grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  }

  .suggestion-card {
    padding: 0.85rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.87rem;
    line-height: 1.45;
    text-align: left;
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      background-color 0.15s ease;
  }

  .suggestion-card:hover {
    border-color: rgba(var(--primary-color-rgb), 0.5);
    background-color: rgba(var(--primary-color-rgb), 0.05);
  }
</style>
