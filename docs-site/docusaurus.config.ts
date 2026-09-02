import fs from 'node:fs';
import path from 'node:path';
import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

// The released version, single-sourced from the repo-root VERSION file so the
// homepage badge can never drift from what was actually shipped.
// The Docker build context is ./docs-site, so ../VERSION is unreachable there —
// opentr.sh passes it through as the OT_VERSION build arg instead. An empty
// string is a valid result: the badge is simply omitted rather than showing a lie.
function resolveVersion(): string {
  // opentr.sh sets APP_VERSION (and therefore OT_VERSION) to the literal "unknown"
  // when VERSION is unreadable, so validate the shape rather than trusting the value.
  const normalize = (raw: string | undefined): string => {
    const trimmed = raw?.trim().replace(/^v/, '') ?? '';
    return /^\d+\.\d+\.\d+/.test(trimmed) ? trimmed : '';
  };

  const fromEnv = normalize(process.env.OT_VERSION);
  if (fromEnv) return fromEnv;
  try {
    return normalize(fs.readFileSync(path.join(__dirname, '..', 'VERSION'), 'utf8'));
  } catch {
    return '';
  }
}

const version = resolveVersion();
const githubRepo = 'https://github.com/attevon-llc/OpenTranscribe';

// When building for in-app embedding (DOCS_BASE_URL=/docs/), the NGINX proxy strips
// the /docs/ prefix before forwarding to this container. So with routeBasePath='docs'
// (the default), pages would live at /docs/docs/... which 404s after the proxy strips /docs/.
// Setting routeBasePath='' places pages at /{page-path} in the build output, which after
// NGINX strips /docs/ correctly resolves to /docs/{page-path} in the browser.
const isEmbedded = process.env.DOCS_BASE_URL === '/docs/';
// In embedded mode, links must NOT include the /docs/ prefix — Docusaurus adds the
// baseUrl automatically. On the public site (baseUrl='/'), /docs/ prefix is needed.
const docsPrefix = isEmbedded ? '' : '/docs';

const config: Config = {
  title: 'OpenTranscribe',
  tagline: 'AI-Powered Transcription and Media Analysis Platform',
  favicon: 'img/favicon.ico',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Enable Mermaid diagrams in markdown
  markdown: {
    mermaid: true,
  },
  themes: ['@docusaurus/theme-mermaid'],

  // Set the production url of your site here
  url: 'https://docs.opentranscribe.app',
  // Set the /<baseUrl>/ pathname under which your site is served
  // DOCS_BASE_URL env var allows building with /docs/ prefix for in-app embedding
  // (the Docker build sets this to /docs/ so internal links work when proxied at /docs/)
  // The public site at docs.opentranscribe.app builds with the default '/'
  baseUrl: process.env.DOCS_BASE_URL || '/',

  // GitHub pages deployment config.
  // If you aren't using GitHub pages, you don't need these.
  organizationName: 'attevon-llc', // Usually your GitHub org/user name.
  projectName: 'OpenTranscribe', // Usually your repo name.

  // Fail the build on a bad cross-reference rather than shipping a dead link.
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  // Exposed to the client via useDocusaurusContext().siteConfig.customFields
  customFields: {
    version,
    githubRepo,
  },

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl:
            `${githubRepo}/tree/master/docs-site/`,
          routeBasePath: isEmbedded ? '' : 'docs',
        },
        blog: {
          showReadingTime: true,
          blogTitle: 'OpenTranscribe Blog',
          blogDescription: 'Updates, releases, and insights about OpenTranscribe development',
          postsPerPage: 'ALL',
          editUrl:
            `${githubRepo}/tree/master/docs-site/`,
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // Replace with your project's social card
    image: 'img/opentranscribe-social-card.png',
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    // This site is built and deployed from the default branch (deploy-docs.yml), while
    // the installer deliberately installs the latest published RELEASE. Those are two
    // different points in history, so the docs can legitimately describe features that
    // have not shipped yet. Say so rather than letting a reader discover it.
    //
    // No version number here on purpose: it would be a hand-maintained fact that rots,
    // and the repo's rule is that version facts are DERIVED, never recorded. The link
    // sends readers to the release list, which is always current.
    announcementBar: {
      id: 'active-development',
      content:
        'OpenTranscribe is under active development. These docs track the default branch and may ' +
        'describe features not yet in the latest ' +
        '<a target="_blank" rel="noopener" href="https://github.com/attevon-llc/OpenTranscribe/releases">release</a>.',
      isCloseable: true,
    },
    navbar: {
      title: 'OpenTranscribe',
      logo: {
        alt: 'OpenTranscribe Logo',
        src: 'img/logo.png',
        srcDark: 'img/logo-dark.png',
      },
      items: [
        {
          to: `${docsPrefix}/getting-started/introduction`,
          position: 'left',
          label: 'Docs',
        },
        // TODO: Uncomment when API pages are created
        // {
        //   type: 'docSidebar',
        //   sidebarId: 'apiSidebar',
        //   position: 'left',
        //   label: 'API Reference',
        // },
        {to: '/roadmap', label: 'Roadmap', position: 'left'},
        {to: '/blog', label: 'Blog', position: 'left'},
        {
          href: githubRepo,
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentation',
          items: [
            {
              label: 'Getting Started',
              to: `${docsPrefix}/getting-started/introduction`,
            },
            {
              label: 'Installation',
              to: `${docsPrefix}/installation/docker-compose`,
            },
            {
              label: 'FAQ',
              to: `${docsPrefix}/faq`,
            },
            // TODO: Add when pages are created
            // {
            //   label: 'User Guide',
            //   to: '/docs/user-guide/uploading-files',
            // },
            // {
            //   label: 'API Reference',
            //   to: '/docs/api/authentication',
            // },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'GitHub Discussions',
              href: `${githubRepo}/discussions`,
            },
            {
              label: 'GitHub Issues',
              href: `${githubRepo}/issues`,
            },
            {
              label: 'GitHub Repository',
              href: githubRepo,
            },
            // TODO: Add when page is created
            // {
            //   label: 'Contributing',
            //   to: '/docs/developer-guide/contributing',
            // },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'Blog',
              to: '/blog',
            },
            {
              label: 'GitHub',
              href: githubRepo,
            },
            {
              label: 'Docker Hub',
              href: 'https://hub.docker.com/u/davidamacey',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} OpenTranscribe. Open Source under AGPL-3.0 License.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
