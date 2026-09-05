<script lang="ts">
  import { t } from '$stores/locale';
  import axios from 'axios';
  import BaseModal from './ui/BaseModal.svelte';

  export let showModal = false;

  // Compile-time constants (vite.config.ts `define`) — the identity of the bundle this
  // tab is actually running, which can lag the server after a deploy.
  const buildVersion = __APP_VERSION__;
  const buildTime = __BUILD_TIME__;

  let serverVersion = '';

  // The backend reports the repo-root VERSION file verbatim (`v0.4.1`) while
  // package.json carries a bare semver (`0.4.1`), and it falls back to the literal
  // "unknown" when neither the file nor $APP_VERSION is present. Compare on the
  // normalized form and stay silent when the server can't identify itself — a false
  // "you are out of date" is worse than no warning at all.
  const normalizeVersion = (value: string) => value.trim().replace(/^v/i, '');

  $: versionMismatch =
    Boolean(serverVersion) &&
    serverVersion !== 'unknown' &&
    normalizeVersion(serverVersion) !== normalizeVersion(buildVersion);

  function closeModal() {
    showModal = false;
  }

  async function fetchVersion() {
    try {
      const res = await axios.get('/health');
      serverVersion = res.data?.version || '';
    } catch {
      serverVersion = '';
    }
  }

  $: if (showModal && !serverVersion) {
    fetchVersion();
  }

</script>

<div class="about-modal-wrapper">
<BaseModal isOpen={showModal} onClose={closeModal} maxWidth="800px" zIndex={1300}>
  <svelte:fragment slot="header">
    <div class="header-title-group">
      <h2 class="modal-title">{$t('about.title')}</h2>
      <div class="header-version">
        <span class="header-version-number" title={buildTime}>
          {$t('about.buildVersion')} {buildVersion}
        </span>
        {#if serverVersion}
          <span class="header-version-sep">&middot;</span>
          <span class="header-version-number" class:mismatch={versionMismatch}>
            {$t('about.serverVersion')} {serverVersion}
          </span>
        {/if}
        <span class="header-version-sep">&middot;</span>
        <span class="header-version-tagline">{$t('about.version')}</span>
      </div>
    </div>
  </svelte:fragment>

  <div class="about-content">
    {#if versionMismatch}
      <p class="version-mismatch" role="status">{$t('about.versionMismatch')}</p>
    {/if}

    <section class="intro-section">
      <p class="subtitle">{$t('about.subtitle')}</p>
              <p class="description">
                {$t('about.description')}
              </p>
            </section>

            <section class="features-section">
              <h3>{$t('about.keyFeatures')}</h3>
              <div class="features-grid">
                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <circle cx="12" cy="12" r="6"></circle>
                      <circle cx="12" cy="12" r="2"></circle>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.aiTranscription')}</h4>
                    <p>{$t('about.aiTranscriptionDesc')}</p>
                  </div>
                </div>

                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                      <circle cx="9" cy="7" r="4"></circle>
                      <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                      <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.speakerDetection')}</h4>
                    <p>{$t('about.speakerDetectionDesc')}</p>
                  </div>
                </div>

                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="11" cy="11" r="8"></circle>
                      <path d="m21 21-4.35-4.35"></path>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.fullTextSearch')}</h4>
                    <p>{$t('about.fullTextSearchDesc')}</p>
                  </div>
                </div>

                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h4a2 2 0 0 1 2 2v1.28c.6.35 1 .98 1 1.72 0 .74-.4 1.38-1 1.72V15a2 2 0 0 1-2 2h-4v1.27c.6.35 1 .98 1 1.73a2 2 0 1 1-4 0c0-.75.4-1.38 1-1.73V17H7a2 2 0 0 1-2-2v-1.28C4.4 13.38 4 12.74 4 12c0-.74.4-1.37 1-1.72V9a2 2 0 0 1 2-2h4V5.73C10.4 5.39 10 4.74 10 4a2 2 0 0 1 2-2z"></path>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.llmSummaries')}</h4>
                    <p>{$t('about.llmSummariesDesc')}</p>
                  </div>
                </div>

                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
                      <line x1="8" y1="21" x2="16" y2="21"></line>
                      <line x1="12" y1="17" x2="12" y2="21"></line>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.progressiveWebApp')}</h4>
                    <p>{$t('about.progressiveWebAppDesc')}</p>
                  </div>
                </div>

                <div class="feature-item">
                  <div class="feature-icon">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                      <circle cx="12" cy="16" r="1"></circle>
                      <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                    </svg>
                  </div>
                  <div>
                    <h4>{$t('about.securePrivate')}</h4>
                    <p>{$t('about.securePrivateDesc')}</p>
                  </div>
                </div>
              </div>
            </section>

            <section class="workflow-section">
              <h3>{$t('about.howItWorks')}</h3>
              <div class="workflow-steps">
                <div class="step">
                  <div class="step-number">1</div>
                  <div class="step-content">
                    <h4>{$t('about.stepUpload')}</h4>
                    <p>{$t('about.stepUploadDesc')}</p>
                  </div>
                </div>

                <div class="step">
                  <div class="step-number">2</div>
                  <div class="step-content">
                    <h4>{$t('about.stepProcess')}</h4>
                    <p>{$t('about.stepProcessDesc')}</p>
                  </div>
                </div>

                <div class="step">
                  <div class="step-number">3</div>
                  <div class="step-content">
                    <h4>{$t('about.stepEnhance')}</h4>
                    <p>{$t('about.stepEnhanceDesc')}</p>
                  </div>
                </div>

                <div class="step">
                  <div class="step-number">4</div>
                  <div class="step-content">
                    <h4>{$t('about.stepExplore')}</h4>
                    <p>{$t('about.stepExploreDesc')}</p>
                  </div>
                </div>
              </div>
            </section>

            <section class="credits-section">
              <h3>{$t('about.builtWith')}</h3>
              <p class="credits-intro">{$t('about.builtWithIntro')}</p>

              <div class="credits-grid">
                <div class="credit-category">
                  <h4>{$t('about.aiMachineLearning')}</h4>
                  <div class="credit-links">
                    <a href="https://github.com/m-bain/whisperX" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.whisperx')}
                    </a>
                    <a href="https://github.com/SYSTRAN/faster-whisper" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.fasterWhisper')}
                    </a>
                    <a href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.pyannote')}
                    </a>
                    <a href="https://github.com/avencera/speakrs" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.speakrs')}
                    </a>
                    <a href="https://huggingface.co/prithivMLmods/Common-Voice-Gender-Detection" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.genderDetection')}
                    </a>
                  </div>
                </div>

                <div class="credit-category">
                  <h4>{$t('about.backendInfrastructure')}</h4>
                  <div class="credit-links">
                    <a href="https://github.com/python/cpython" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.python')}
                    </a>
                    <a href="https://github.com/fastapi/fastapi" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.fastapi')}
                    </a>
                    <a href="https://github.com/postgres/postgres" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.postgresql')}
                    </a>
                  </div>
                </div>

                <div class="credit-category">
                  <h4>{$t('about.frontend')}</h4>
                  <div class="credit-links">
                    <a href="https://github.com/sveltejs/svelte" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.svelte')}
                    </a>
                    <a href="https://github.com/microsoft/TypeScript" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.typescript')}
                    </a>
                  </div>
                </div>

                <div class="credit-category">
                  <h4>{$t('about.queueTaskProcessing')}</h4>
                  <div class="credit-links">
                    <a href="https://github.com/celery/celery" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.celery')}
                    </a>
                    <a href="https://github.com/redis/redis" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.redis')}
                    </a>
                    <a href="https://github.com/mher/flower" target="_blank" rel="noopener noreferrer" class="credit-link">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                      </svg>
                      {$t('about.credits.flower')}
                    </a>
                  </div>
                </div>
              </div>

              <p class="credits-footer">
                {$t('about.gratefulMessage')}
                <a href="https://github.com/openai/whisper" target="_blank" rel="noopener noreferrer" class="inline-link">OpenAI Whisper</a>
                {$t('about.whisperFoundation')}
                <a href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noopener noreferrer" class="inline-link">pyannote/speaker-diarization-community-1</a>
                {$t('about.pyannoteFoundation')}
              </p>
            </section>

  </div>
</BaseModal>
</div>

<style>
  .header-title-group {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-primary, #1a1a1a);
  }

  .about-modal-wrapper :global(.modal-body) {
    padding: 2rem !important;
  }

  .about-content {
    display: flex;
    flex-direction: column;
    gap: 2rem;
  }

  .subtitle {
    font-size: 1.125rem;
    color: var(--primary-color);
    font-weight: 500;
    margin: 0 0 1rem 0;
  }

  .description {
    color: var(--text-secondary);
    line-height: 1.6;
    margin: 0;
  }

  .features-section h3,
  .workflow-section h3 {
    color: var(--text-color);
    font-size: 1.25rem;
    font-weight: 600;
    margin: 0 0 1rem 0;
  }

  .features-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
    gap: 1rem;
  }

  .feature-item {
    display: flex;
    gap: 0.75rem;
    align-items: flex-start;
  }

  .feature-icon {
    font-size: 1.5rem;
    flex-shrink: 0;
  }

  .feature-item h4 {
    color: var(--text-color);
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
  }

  .feature-item p {
    color: var(--text-secondary);
    font-size: 0.85rem;
    line-height: 1.5;
    margin: 0;
  }

  .workflow-steps {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .step {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
  }

  .step-number {
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    background: #3b82f6;
    color: white;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 600;
    font-size: 0.875rem;
  }

  .step-content h4 {
    color: var(--text-color);
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
  }

  .step-content p {
    color: var(--text-secondary);
    font-size: 0.85rem;
    line-height: 1.5;
    margin: 0;
  }

  .header-version {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .header-version-number {
    color: var(--text-color, var(--text-primary, #1a1a1a));
    font-size: 0.9rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .header-version-number.mismatch {
    color: var(--warning-text, var(--warning-color));
    font-weight: 600;
  }

  .header-version-sep {
    color: var(--text-secondary);
    opacity: 0.5;
    font-size: 0.9rem;
  }

  .version-mismatch {
    margin: 0;
    padding: 0.75rem 1rem;
    border: 1px solid var(--warning-border);
    border-radius: 6px;
    background: var(--warning-bg);
    color: var(--warning-text, var(--text-primary));
    font-size: 0.9rem;
    line-height: 1.5;
  }

  .header-version-tagline {
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  /* Credits Section Styles */
  .credits-section h3 {
    color: var(--text-color);
    font-size: 1.25rem;
    font-weight: 600;
    margin: 0 0 1rem 0;
  }

  .credits-intro {
    color: var(--text-secondary);
    line-height: 1.6;
    margin: 0 0 1.5rem 0;
  }

  .credits-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.5rem;
    margin-bottom: 2rem;
  }

  .credit-category h4 {
    color: var(--text-color);
    font-size: 1rem;
    font-weight: 600;
    margin: 0 0 0.75rem 0;
  }

  .credit-links {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .credit-link {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.9rem;
    line-height: 1.5;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    transition: all 0.2s ease;
  }

  .credit-link:hover {
    color: var(--primary-color);
    background: var(--hover-color, rgba(59, 130, 246, 0.1));
    transform: translateX(4px);
  }

  .credit-link svg {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .credit-link:hover svg {
    opacity: 1;
  }

  .credits-footer {
    color: var(--text-secondary);
    font-size: 0.9rem;
    line-height: 1.6;
    text-align: center;
    margin: 0;
  }

  .inline-link {
    color: var(--primary-color);
    text-decoration: none;
    font-weight: 500;
  }

  .inline-link:hover {
    text-decoration: underline;
  }

  /* Mobile responsive */
  @media (max-width: 768px) {
    .about-modal-wrapper :global(.modal-body) {
      padding: 1rem !important;
    }

    .about-content {
      gap: 1.25rem;
    }

    .features-grid {
      grid-template-columns: 1fr;
      gap: 0.75rem;
    }

    /* Keep steps as rows (number left, text right) — stacking them vertically
       wastes too much space on a small screen */
    .step {
      gap: 0.75rem;
    }

    .workflow-steps {
      gap: 0.75rem;
    }

    /* Credits responsive adjustments */
    .credits-grid {
      grid-template-columns: 1fr;
      gap: 1rem;
      margin-bottom: 1rem;
    }

    .credit-link {
      min-height: 44px;
    }
  }
</style>
