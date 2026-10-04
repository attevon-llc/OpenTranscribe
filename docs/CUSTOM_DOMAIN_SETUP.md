# Custom Domain Setup: docs.opentranscribe.io

Complete guide to setting up docs.opentranscribe.io for your documentation site while keeping your main site (opentranscribe.io) on Squarespace.

## Overview

**Architecture:**
```
opentranscribe.io (Squarespace)
    ↓
    Main website, marketing, blog, etc.

docs.opentranscribe.io (GitHub Pages)
    ↓
    Documentation site (Docusaurus)
```

## Prerequisites

✅ Domain registered (opentranscribe.io)
✅ Squarespace site set up
✅ GitHub repository with documentation
✅ DNS access in Squarespace

## Step 1: Configure Docusaurus (✅ DONE)

I've already updated the Docusaurus configuration:

**File:** `docs-site/docusaurus.config.ts`
```typescript
url: 'https://docs.opentranscribe.io',
baseUrl: '/',
```

**File:** `docs-site/static/CNAME`
```
docs.opentranscribe.io
```

These changes tell Docusaurus and GitHub Pages to serve the site at docs.opentranscribe.io.

## Step 2: Enable GitHub Pages

### 2.1. Push Your Changes

First, push the documentation site to GitHub:

```bash
git add .
git commit -m "docs: Configure custom domain docs.opentranscribe.io"
git push origin master
```

### 2.2. Enable GitHub Pages in Repository Settings

1. **Go to your GitHub repository**: https://github.com/attevon-llc/OpenTranscribe
2. **Click "Settings"** (top right)
3. **Click "Pages"** (left sidebar)
4. **Configure Source**:
   - Source: **GitHub Actions**
   - (This is already set up by the workflow I created)

### 2.3. Trigger the Documentation Build

The GitHub Actions workflow will automatically run when you push changes to the `docs-site/` directory. You can also manually trigger it:

1. Go to **Actions** tab
2. Click **"Deploy Documentation"** workflow
3. Click **"Run workflow"** → **"Run workflow"**

Wait 2-3 minutes for the build to complete.

### 2.4. Verify GitHub Pages is Active

1. Go back to **Settings** → **Pages**
2. You should see: **"Your site is published at https://docs.opentranscribe.io"**
   - Note: This won't work yet until DNS is configured

## Step 3: Configure DNS in Squarespace

### 3.1. Log into Squarespace

1. Go to https://account.squarespace.com/
2. Click on your **opentranscribe.io** site
3. Go to **Settings** → **Domains**

### 3.2. Access DNS Settings

1. Click on **opentranscribe.io**
2. Click **"DNS Settings"** or **"Advanced Settings"**
3. You should see a list of DNS records

### 3.3. Add CNAME Record for docs Subdomain

Add a **new CNAME record** with these exact values:

```
Type:  CNAME
Host:  docs
Data:  davidamacey.github.io
TTL:   3600 (or leave default)
```

**Visual Guide:**
```
┌─────────┬──────┬─────────────────────────┬──────┐
│  Type   │ Host │         Data            │ TTL  │
├─────────┼──────┼─────────────────────────┼──────┤
│  CNAME  │ docs │ davidamacey.github.io   │ 3600 │
└─────────┴──────┴─────────────────────────┴──────┘
```

**Important Notes:**
- **Host:** Use just `docs`, not `docs.opentranscribe.io` (Squarespace adds your domain automatically)
- **Data:** Must be `davidamacey.github.io` (your GitHub Pages URL)
- **Do NOT use an A record** - GitHub Pages requires CNAME for custom subdomains

### 3.4. Squarespace-Specific Instructions

**If you see "Custom Records" section:**
1. Click **"Add Record"**
2. Select **"CNAME"** from dropdown
3. Enter:
   - **Record:** `docs`
   - **Value:** `davidamacey.github.io`
4. Click **"Save"**

**If you see "DNS Records" table:**
1. Click **"+ Add"** or **"Add Record"**
2. Choose **"CNAME Record"**
3. Fill in:
   - **Name/Host:** `docs`
   - **Content/Value:** `davidamacey.github.io`
4. Click **"Add"** or **"Save"**

### 3.5. Example Screenshots (What You Should See)

```
Squarespace DNS Settings
════════════════════════════════════════════════════

Your Domain: opentranscribe.io
Status: Active

DNS Records:
┌─────────┬──────────┬─────────────────────────┬──────┐
│  Type   │   Host   │         Data            │ TTL  │
├─────────┼──────────┼─────────────────────────┼──────┤
│    A    │    @     │  198.185.159.144        │ 3600 │
│    A    │    @     │  198.185.159.145        │ 3600 │
│  CNAME  │   www    │  ext-cust.squarespace   │ 3600 │
│  CNAME  │   docs   │  davidamacey.github.io  │ 3600 │ ← NEW
└─────────┴──────────┴─────────────────────────┴──────┘
```

## Step 4: Verify DNS Propagation

DNS changes can take 5 minutes to 48 hours to propagate globally. Here's how to check:

### 4.1. Check DNS with Command Line

**On Mac/Linux:**
```bash
# Check if CNAME is set
dig docs.opentranscribe.io CNAME +short

# Expected output:
# davidamacey.github.io.
```

**On Windows (PowerShell):**
```powershell
Resolve-DnsName docs.opentranscribe.io -Type CNAME

# Expected output should show davidamacey.github.io
```

### 4.2. Online DNS Checker

Use online tools to check DNS propagation globally:

1. **DNS Checker**: https://dnschecker.org/
   - Enter: `docs.opentranscribe.io`
   - Type: `CNAME`
   - Click "Search"
   - Should show `davidamacey.github.io` globally

2. **What's My DNS**: https://whatsmydns.net/
   - Enter: `docs.opentranscribe.io`
   - Type: `CNAME`
   - Should show green checkmarks worldwide

### 4.3. Check GitHub Pages Status

1. Go to **GitHub Repository** → **Settings** → **Pages**
2. You should see:
   ```
   ✓ Your site is live at https://docs.opentranscribe.io
   ```
3. If you see a warning about DNS, wait 10-30 minutes and refresh

## Step 5: Enable HTTPS

GitHub Pages automatically provides free SSL certificates via Let's Encrypt.

### 5.1. Wait for Certificate Provisioning

After DNS propagates:
1. GitHub will automatically detect your custom domain
2. It will provision an SSL certificate (takes 10-60 minutes)
3. You'll see a checkmark: **"HTTPS enforced"**

### 5.2. Verify HTTPS is Working

1. Go to **Settings** → **Pages** in your GitHub repo
2. Check the box: **"Enforce HTTPS"** (should be automatic)
3. Visit https://docs.opentranscribe.io
4. You should see a **padlock icon** in the browser

## Step 6: Test Your Documentation Site

### 6.1. Access Your Documentation

Open your browser and go to:
```
https://docs.opentranscribe.io
```

You should see the OpenTranscribe documentation homepage!

### 6.2. Test All URLs

Make sure these work:
- https://docs.opentranscribe.io
- https://docs.opentranscribe.io/docs/getting-started/introduction
- https://docs.opentranscribe.io/docs/getting-started/quick-start
- https://docs.opentranscribe.io/docs/faq

### 6.3. Test Redirects

HTTP should redirect to HTTPS:
- http://docs.opentranscribe.io → https://docs.opentranscribe.io ✓

## Troubleshooting

### Problem: DNS Not Resolving

**Symptoms:**
- `dig docs.opentranscribe.io` returns no results
- DNS checkers show no records

**Solutions:**
1. **Check Squarespace DNS settings**:
   - Verify CNAME record is saved
   - Host should be `docs` (not `docs.opentranscribe.io`)
   - Data should be `davidamacey.github.io` (not a URL with https://)

2. **Wait for propagation**:
   - DNS changes take 5-30 minutes minimum
   - Can take up to 48 hours globally
   - Check with `dig` or online tools

3. **Check Squarespace domain status**:
   - Ensure opentranscribe.io is active
   - Verify domain is not expired
   - Check for any domain warnings

### Problem: "404 - There isn't a GitHub Pages site here"

**Symptoms:**
- DNS resolves correctly
- But page shows GitHub 404 error

**Solutions:**
1. **Verify CNAME file exists**:
   ```bash
   cat docs-site/static/CNAME
   # Should show: docs.opentranscribe.io
   ```

2. **Check GitHub Pages is enabled**:
   - Go to Settings → Pages
   - Source should be "GitHub Actions"
   - Should show "Your site is published at..."

3. **Rebuild documentation**:
   ```bash
   cd docs-site
   npm run build
   ```
   - Commit and push changes
   - Wait for GitHub Actions to complete

4. **Clear GitHub Pages cache**:
   - Go to Settings → Pages
   - Click "Remove" next to custom domain
   - Wait 5 minutes
   - Let GitHub Actions redeploy (it will detect CNAME file)

### Problem: HTTPS Not Working

**Symptoms:**
- Site loads over HTTP but not HTTPS
- Certificate errors

**Solutions:**
1. **Wait for certificate provisioning**:
   - Takes 10-60 minutes after DNS propagates
   - GitHub provisions Let's Encrypt certificate automatically

2. **Check GitHub Pages settings**:
   - Go to Settings → Pages
   - Ensure "Enforce HTTPS" is checked
   - If grayed out, wait for certificate provisioning

3. **Verify DNS is correct**:
   - Must use CNAME (not A record) for subdomains
   - Value must be `davidamacey.github.io`

### Problem: Old Content Showing

**Symptoms:**
- Site loads but shows outdated content
- Changes not appearing

**Solutions:**
1. **Clear browser cache**:
   - Hard refresh: Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (Mac)
   - Or open in incognito/private window

2. **Check GitHub Actions**:
   - Go to Actions tab
   - Verify latest deployment succeeded
   - Check logs for errors

3. **Verify build includes new content**:
   ```bash
   cd docs-site
   npm run build
   ls build/docs/  # Should show your markdown files
   ```

## Updating Documentation

Once set up, updates are automatic:

1. **Make changes** to documentation in `docs-site/docs/`
2. **Commit and push**:
   ```bash
   git add docs-site/
   git commit -m "docs: Update getting started guide"
   git push origin master
   ```
3. **GitHub Actions automatically**:
   - Builds the documentation
   - Deploys to docs.opentranscribe.io
   - Takes 2-3 minutes

## DNS Record Summary

After setup, your DNS should look like this:

```
Domain: opentranscribe.io (Squarespace)
├── @ (root)          → Squarespace (A records)
├── www               → Squarespace (CNAME)
└── docs              → GitHub Pages (CNAME to davidamacey.github.io)
```

**Main site:** https://opentranscribe.io → Squarespace
**Documentation:** https://docs.opentranscribe.io → GitHub Pages

## Security Best Practices

### 1. HTTPS Only
- ✅ Enforced by GitHub Pages
- ✅ Free SSL certificate from Let's Encrypt
- ✅ Automatic renewal

### 2. HSTS (Optional but Recommended)
Add this to your documentation homepage to enforce HTTPS:
```html
Strict-Transport-Security: max-age=31536000; includeSubDomains
```
(GitHub Pages may add this automatically)

### 3. Content Security Policy (Optional)
Can be added via meta tags in Docusaurus config for extra security.

## Linking Between Sites

### From Main Site (Squarespace) to Docs

Add a navigation link in Squarespace:
```
Text: Documentation
URL:  https://docs.opentranscribe.io
```

### From Docs to Main Site

Already configured in `docusaurus.config.ts` footer:
```typescript
{
  label: 'Main Website',
  href: 'https://opentranscribe.io',
},
```

## Monitoring

### Check Site Health
- **GitHub Actions**: Monitor builds at https://github.com/attevon-llc/OpenTranscribe/actions
- **GitHub Pages Status**: Settings → Pages shows site status
- **Uptime Monitoring**: Use services like UptimeRobot (free) to monitor docs.opentranscribe.io

### Analytics (Optional)
Add Google Analytics to track documentation usage:

1. Get Google Analytics ID
2. Add to `docusaurus.config.ts`:
   ```typescript
   gtag: {
     trackingID: 'G-XXXXXXXXXX',
   },
   ```

## Cost Breakdown

- **Domain (opentranscribe.io)**: $12-20/year (you already have this)
- **Squarespace hosting**: $16-40/month (you already have this)
- **GitHub Pages**: **FREE** (for public repositories)
- **SSL Certificate**: **FREE** (Let's Encrypt via GitHub Pages)
- **DNS**: **FREE** (included with Squarespace)

**Total additional cost: $0/year** 🎉

## Support

If you encounter issues:

1. **Squarespace DNS Help**: https://support.squarespace.com/hc/en-us/articles/360002101888
2. **GitHub Pages Help**: https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site
3. **Docusaurus Help**: https://docusaurus.io/docs/deployment

## Quick Reference Commands

```bash
# Check DNS
dig docs.opentranscribe.io CNAME +short

# Test site locally
cd docs-site && npm start

# Build documentation
cd docs-site && npm run build

# Deploy (automatic on push to master)
git add . && git commit -m "docs: Update" && git push

# Check GitHub Actions status
gh run list --workflow=deploy-docs.yml
```

## Next Steps After Setup

Once docs.opentranscribe.io is live:

1. ✅ Add link from main site to documentation
2. ✅ Set up Google Analytics (optional)
3. ✅ Monitor first 24 hours for any issues
4. ✅ Share the docs link on social media
5. ✅ Update README.md to point to new docs URL

---

**Setup Status:** ✅ Docusaurus configured, CNAME created
**Next:** Configure DNS in Squarespace (Step 3)

**Questions?** Create an issue on GitHub or check the support links above.
