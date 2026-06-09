import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

/**
 * Creating a sidebar enables you to:
 - create an ordered group of docs
 - render a sidebar for each doc of that group
 - provide next/previous navigation

 The sidebars can be generated from the filesystem, or explicitly defined here.

 Create as many sidebars as you want.
 */
const sidebars: SidebarsConfig = {
  // Main documentation sidebar
  docsSidebar: [
    {
      type: 'category',
      label: 'Getting Started',
      items: [
        'getting-started/introduction',
        'getting-started/quick-start',
        'getting-started/first-transcription',
      ],
    },
    {
      type: 'category',
      label: 'Installation',
      items: [
        'installation/docker-compose',
        'installation/hardware-requirements',
        'installation/gpu-setup',
        'installation/huggingface-setup',
        'installation/offline-installation',
        'installation/troubleshooting',
      ],
    },
    {
      type: 'category',
      label: 'User Guide',
      items: [
        'user-guide/uploading-files',
        'user-guide/watch-sources',
        'user-guide/speaker-management',
        'user-guide/ai-summarization',
        'user-guide/search-and-filters',
        'user-guide/collections',
        'user-guide/admin-panel',
      ],
    },
    {
      type: 'category',
      label: 'Features',
      items: [
        'features/transcription',
        'features/speaker-diarization',
        'features/boundary-correction',
        'features/content-redaction',
        'features/watch-sources',
        'features/llm-integration',
        'features/authentication',
        'features/pipeline-optimization',
      ],
    },
    {
      type: 'category',
      label: 'Authentication',
      items: [
        'authentication/overview',
        // Detailed guides are in main docs/ folder, linked from overview
      ],
    },
    {
      type: 'category',
      label: 'Configuration',
      items: [
        'configuration/environment-variables',
        'configuration/multi-gpu-scaling',
        'configuration/nginx-setup',
        'configuration/neural-search-setup',
        'configuration/embedding-migration',
      ],
    },
    {
      type: 'category',
      label: 'Operations',
      items: [
        'operations/production-deployment',
        'operations/fresh-deployments',
        'operations/backup-restore',
        'operations/storage-recovery',
        'operations/backup-audit',
        'operations/upgrading',
        'operations/monitoring',
        'operations/performance-tuning',
        'operations/prompt-engineering',
        'operations/security-hardening',
        'operations/runbooks',
      ],
    },
    {
      type: 'category',
      label: 'Developer Guide',
      items: [
        'developer-guide/architecture',
        'developer-guide/diarization-boundary-correction',
        'developer-guide/watch-sources',
        'developer-guide/contributing',
        'developer-guide/testing',
      ],
    },
    'faq',
  ],

  // API Reference sidebar - commented out until pages are created
  // apiSidebar: [
  //   {
  //     type: 'category',
  //     label: 'API Reference',
  //     items: [
  //       'api/authentication',
  //       'api/files',
  //       'api/transcriptions',
  //       'api/speakers',
  //       'api/summaries',
  //       'api/search',
  //       'api/websockets',
  //     ],
  //   },
  // ],
};

export default sidebars;
