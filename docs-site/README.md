# OpenTranscribe Documentation Site

This directory contains the source code for the OpenTranscribe documentation website built with [Docusaurus](https://docusaurus.io/).

## Quick Start

### Installation

```bash
cd docs-site
npm install
```

### Local Development

```bash
npm start
```

This command starts a local development server and opens up a browser window. Most changes are reflected live without having to restart the server.

### Build

```bash
npm run build
```

This command generates static content into the `build` directory and can be served using any static contents hosting service.

### Test Build Locally

```bash
npm run serve
```

This command serves the built website locally to test the production build.

## Project Structure

```
docs-site/
├── blog/                   # Blog posts (announcements, tutorials)
├── docs/                   # Documentation markdown files
│   ├── getting-started/    # Getting started guides
│   ├── installation/       # Installation guides
│   ├── user-guide/         # User-facing documentation
│   ├── features/           # Feature documentation
│   ├── configuration/      # Configuration guides
│   ├── api/                # API reference
│   ├── developer-guide/    # Developer documentation
│   ├── deployment/         # Deployment guides
│   ├── use-cases/          # Use case examples
│   └── faq.md              # Frequently asked questions
├── src/
│   ├── components/         # React components
│   ├── css/                # Custom styles
│   └── pages/              # Custom pages (homepage, etc.)
├── static/
│   ├── img/                # Images and assets
│   └── screenshots/        # Feature screenshots
├── docusaurus.config.ts    # Docusaurus configuration
├── sidebars.ts             # Sidebar navigation
└── package.json
```

## Writing Documentation

### Creating a New Page

1. Create a new Markdown file in the appropriate directory under `docs/`
2. Add frontmatter at the top:

```markdown
---
sidebar_position: 1
title: Page Title
---

# Page Heading

Your content here...
```

3. Update `sidebars.ts` if needed to include the new page in navigation

### Documentation Guidelines

- **Use clear, concise language** - Write for developers of all skill levels
- **Include code examples** - Show, don't just tell
- **Add screenshots** - Visual aids improve understanding
- **Link to related pages** - Help users navigate
- **Keep pages focused** - One topic per page
- **Update regularly** - Keep docs in sync with code

### Markdown Features

Docusaurus supports extended Markdown features:

**Admonitions:**
```markdown
:::tip Pro Tip
This is a helpful tip!
:::

:::warning Warning
Be careful with this!
:::

:::danger Danger
This can break things!
:::

:::info Info
Here's some information.
:::
```

**Code Blocks with Syntax Highlighting:**
````markdown
```bash
./opentranscribe.sh start
```

```python
def hello_world():
    print("Hello, World!")
```
````

**Tabs:**
```markdown
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

<Tabs>
  <TabItem value="npm" label="npm">
    npm install
  </TabItem>
  <TabItem value="yarn" label="Yarn">
    yarn install
  </TabItem>
</Tabs>
```

**Comments, blog truncation and explicit heading IDs — MDX form only.** Every `.md` file
here is compiled as MDX, which parses `{ ... }` as a JavaScript expression and rejects
HTML comments outright. Use the MDX comment form for all three:

```markdown
{/* an ordinary comment */}

{/* truncate */}

## A heading with a stable anchor {/* #explicit-id */}
```

The CommonMark spellings (`<!-- comment -->`, `<!-- truncate -->`, `## Heading {#id}`)
fail the build with `Unexpected character !` or `Could not parse expression with acorn`.
Explicit heading IDs matter because the auto-generated slug changes whenever the heading
text is reworded, silently breaking every inbound link — `onBrokenAnchors: 'throw'` catches
those at build time, but only for links inside this site.

## Deployment

### GitHub Pages (Automatic)

The documentation site automatically deploys to GitHub Pages when changes are pushed to the `master` branch.

**Setup:**
1. Enable GitHub Pages in repository settings
2. Set source to "GitHub Actions"
3. Push changes to `master` branch
4. GitHub Actions will build and deploy automatically

**URL:** `https://docs.opentranscribe.app` (the custom domain in `static/CNAME`)

### Manual Deployment

#### GitHub Pages (Manual)

```bash
# Using SSH
USE_SSH=true npm run deploy

# Using HTTPS
GIT_USER=<Your GitHub username> npm run deploy
```

#### Vercel

1. Connect GitHub repository to Vercel
2. Set root directory to `docs-site`
3. Vercel auto-deploys on every push

#### Cloudflare Pages

1. Connect GitHub repository to Cloudflare Pages
2. Set build command: `npm run build`
3. Set build output directory: `build`
4. Set root directory: `docs-site`

### Custom Domain

To use a custom domain (e.g., docs.opentranscribe.io):

1. Update `url` in `docusaurus.config.ts`:
   ```ts
   url: 'https://docs.opentranscribe.io',
   ```

2. Add `CNAME` file to `static/` directory:
   ```
   docs.opentranscribe.io
   ```

3. Configure DNS:
   - Add CNAME record pointing to GitHub Pages or your hosting provider
   - Wait for DNS propagation (can take 24-48 hours)

## Maintenance

### Updating Dependencies

```bash
npm update
```

### Checking for Broken Links

```bash
npm run build
```

Docusaurus will error on broken internal links during build.

### Adding Images

1. Place images in `static/img/` or `static/screenshots/`
2. Reference in Markdown:
   ```markdown
   ![Alt text](/img/screenshot.png)
   ```

### Blog Posts

Create blog posts in `blog/` directory:

```markdown
---
slug: welcome
title: Welcome to OpenTranscribe
authors: [your-name]
tags: [announcement, release]
---

Your blog post content here...
```

## Versioning (Future)

When OpenTranscribe needs version-specific docs:

```bash
npm run docusaurus docs:version 1.0
```

This creates a snapshot of docs for version 1.0 that remains unchanged.

## Translations (Future)

To add translations:

1. Update `i18n` config in `docusaurus.config.ts`
2. Create translation files:
   ```bash
   npm run write-translations -- --locale fr
   ```
3. Translate files in `i18n/fr/docusaurus-plugin-content-docs/current/`

## Contributing

See [CONTRIBUTING.md](../docs/CONTRIBUTING.md) for guidelines on contributing to the documentation.

## Resources

- [Docusaurus Documentation](https://docusaurus.io/docs)
- [Markdown Guide](https://www.markdownguide.org/)
- [Docusaurus Best Practices](https://docusaurus.io/docs/category/guides)

## Support

- **GitHub Issues**: [Report documentation issues](https://github.com/davidamacey/OpenTranscribe/issues)
- **GitHub Discussions**: [Ask questions](https://github.com/davidamacey/OpenTranscribe/discussions)
