# Squarespace DNS Quick Start Guide

**Goal:** Set up docs.opentranscribe.io to point to your GitHub Pages documentation site.

## Visual Overview

```
Your Setup:
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  opentranscribe.io (Squarespace)                       │
│  ├── Main website, marketing, etc.                     │
│  └── DNS managed in Squarespace                        │
│                                                         │
│  docs.opentranscribe.io (GitHub Pages)                 │
│  └── Documentation site (Docusaurus)                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## What I've Already Done ✅

1. ✅ Updated Docusaurus config to use `docs.opentranscribe.io`
2. ✅ Created CNAME file for GitHub Pages
3. ✅ GitHub Actions workflow is ready to deploy

## What You Need to Do (5 Steps)

### Step 1: Push Changes to GitHub (2 minutes)

```bash
# In your terminal, in the OpenTranscribe directory
git add .
git commit -m "docs: Configure custom domain docs.opentranscribe.io"
git push origin master
```

### Step 2: Verify GitHub Pages Deployment (3 minutes)

1. Go to: https://github.com/attevon-llc/OpenTranscribe/actions
2. Wait for **"Deploy Documentation"** workflow to complete (green checkmark)
3. Go to: https://github.com/attevon-llc/OpenTranscribe/settings/pages
4. Verify it says: **"Your site is published"**

### Step 3: Configure DNS in Squarespace (5 minutes)

This is the **MOST IMPORTANT** step!

#### 3.1. Log into Squarespace
1. Go to https://account.squarespace.com/
2. Select your **opentranscribe.io** site

#### 3.2. Access DNS Settings
1. Click **Settings** (left sidebar)
2. Click **Domains**
3. Click **opentranscribe.io**
4. Click **DNS Settings** or **Advanced Settings**

#### 3.3. Add CNAME Record

Click **"Add Record"** or **"+ Add"** and enter:

```
┌─────────────────────────────────────────────────┐
│  Type:  CNAME                                   │
│  Host:  docs                                    │
│  Data:  davidamacey.github.io                   │
│  TTL:   3600  (or leave default)                │
└─────────────────────────────────────────────────┘
```

**IMPORTANT:**
- ✅ Host is just `docs` (NOT `docs.opentranscribe.io`)
- ✅ Data is `davidamacey.github.io` (NOT a URL with https://)
- ✅ Use CNAME (NOT A record)

#### 3.4. Save

Click **"Save"** or **"Add Record"**

You should now see in your DNS records list:

```
Type     Host    Data
CNAME    docs    davidamacey.github.io   ← This is new!
```

### Step 4: Wait for DNS Propagation (5-30 minutes)

DNS changes take time to propagate globally.

**Check if DNS is working:**

**Option A: Command Line (Mac/Linux)**
```bash
dig docs.opentranscribe.io CNAME +short
```
Should show: `davidamacey.github.io.`

**Option B: Command Line (Windows PowerShell)**
```powershell
Resolve-DnsName docs.opentranscribe.io -Type CNAME
```

**Option C: Online Tool**
1. Go to https://dnschecker.org/
2. Enter: `docs.opentranscribe.io`
3. Type: `CNAME`
4. Should show `davidamacey.github.io` globally

### Step 5: Access Your Documentation (30-60 minutes after DNS)

Once DNS propagates, visit:
```
https://docs.opentranscribe.io
```

🎉 **You should see your OpenTranscribe documentation!**

## Troubleshooting

### Issue: DNS Not Working After 30 Minutes

**Check Squarespace DNS Record:**
1. Go back to Squarespace → Domains → DNS Settings
2. Verify the CNAME record shows:
   - Host: `docs`
   - Data: `davidamacey.github.io`
3. If wrong, delete and re-add correctly

**Common Mistakes:**
- ❌ Using `docs.opentranscribe.io` as host (should be just `docs`)
- ❌ Using `https://davidamacey.github.io` as data (remove https://)
- ❌ Using an A record instead of CNAME

### Issue: "404 - There isn't a GitHub Pages site here"

**Solution:**
1. Go to https://github.com/attevon-llc/OpenTranscribe/settings/pages
2. Verify it says "Your site is published"
3. Check that CNAME file exists:
   ```bash
   cat docs-site/static/CNAME
   # Should show: docs.opentranscribe.io
   ```
4. Re-run GitHub Actions:
   - Go to Actions tab
   - Click "Deploy Documentation"
   - Click "Run workflow"

### Issue: SSL/HTTPS Not Working

**Solution:** Wait 10-60 minutes after DNS propagates. GitHub automatically provisions SSL certificate from Let's Encrypt.

**Check Status:**
1. Go to https://github.com/attevon-llc/OpenTranscribe/settings/pages
2. Look for "HTTPS" status
3. Should show checkmark when ready

## Visual Guide: Squarespace DNS Settings

### What You'll See (Before Adding Record):

```
Squarespace → Settings → Domains → opentranscribe.io → DNS Settings

┌─────────────────────────────────────────────────────┐
│  DNS Records for opentranscribe.io                  │
├─────────────────────────────────────────────────────┤
│                                                      │
│  Type    Host    Data                         TTL   │
│  ────────────────────────────────────────────────   │
│  A       @       198.185.159.144              3600  │
│  A       @       198.185.159.145              3600  │
│  CNAME   www     ext-cust.squarespace.com    3600  │
│                                                      │
│  [ + Add Record ]                                   │
└─────────────────────────────────────────────────────┘
```

### What You'll See (After Adding Record):

```
┌─────────────────────────────────────────────────────┐
│  DNS Records for opentranscribe.io                  │
├─────────────────────────────────────────────────────┤
│                                                      │
│  Type    Host    Data                         TTL   │
│  ────────────────────────────────────────────────   │
│  A       @       198.185.159.144              3600  │
│  A       @       198.185.159.145              3600  │
│  CNAME   www     ext-cust.squarespace.com    3600  │
│  CNAME   docs    davidamacey.github.io       3600  │ ← NEW!
│                                                      │
│  [ + Add Record ]                                   │
└─────────────────────────────────────────────────────┘
```

## Testing Checklist

After setup, verify these URLs work:

- [ ] https://docs.opentranscribe.io
- [ ] https://docs.opentranscribe.io/docs/getting-started/introduction
- [ ] https://docs.opentranscribe.io/docs/getting-started/quick-start
- [ ] https://docs.opentranscribe.io/docs/faq
- [ ] http://docs.opentranscribe.io (should redirect to https)

## Timeline Expectations

| Step | Time Required |
|------|---------------|
| Push to GitHub | 1 minute |
| GitHub Actions build | 2-3 minutes |
| Configure DNS in Squarespace | 2-5 minutes |
| **DNS propagation** | **5-30 minutes** ⏰ |
| SSL certificate provisioning | 10-60 minutes |
| **Total:** | **20-90 minutes** |

**Pro Tip:** DNS propagation is the waiting game. Go grab coffee while you wait! ☕

## Need Help?

If stuck after following these steps:

1. **Check the full guide:** [CUSTOM_DOMAIN_SETUP.md](./CUSTOM_DOMAIN_SETUP.md)
2. **Squarespace Support:** https://support.squarespace.com/hc/en-us/articles/360002101888
3. **GitHub Issues:** Create an issue with screenshots of:
   - Your Squarespace DNS settings
   - GitHub Pages settings page
   - Output of `dig docs.opentranscribe.io CNAME +short`

## Summary

**Files Updated:**
- ✅ `docs-site/docusaurus.config.ts` - URL changed to docs.opentranscribe.io
- ✅ `docs-site/static/CNAME` - Created with docs.opentranscribe.io

**Your Action Required:**
1. Push changes to GitHub
2. Add CNAME record in Squarespace DNS:
   - Host: `docs`
   - Data: `davidamacey.github.io`
3. Wait for DNS propagation (5-30 minutes)
4. Visit https://docs.opentranscribe.io

**Cost:** $0 (GitHub Pages is free!)

That's it! 🚀
