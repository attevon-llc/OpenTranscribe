# YouTube Anti-Bot Protection - Quick Start

## What Is Implemented

- Cookie Authentication - Use browser cookies for authenticated downloads
- Playlist Staggering - Progressive delays between playlist videos
- Pre-Download Jitter - Random delays before each download
- Rate Limiting - Per-user hourly/daily quotas tracked in Redis
- Deno JS Runtime - Embedded in the download worker container for yt-dlp JavaScript player extraction
- User-Configurable Download Quality - Per-user quality setting (best, 1080p, 720p, 480p, audio-only)
- yt-dlp 2026 best practices - Automatic client rotation, Android/mweb fallbacks

## Immediate Action (If Currently Banned)

### 1. Enable VPN First!

```bash
# Enable PIA VPN, then verify new IP:
curl ifconfig.me
```

### 2. Clear Browser Cookies

Firefox: Settings → Privacy → Clear Data → Cookies
Chrome: Settings → Privacy → Clear Browsing Data → Cookies

### 3. Log into YouTube (with VPN active)

Open browser, go to youtube.com, log in fresh.

### 4. Enable Cookie Authentication

Add to `.env`:

```bash
# Use browser cookies (requires Firefox in container)
YOUTUBE_COOKIE_BROWSER=firefox
```

### 5. Rebuild Backend

```bash
# Add Firefox to backend container first
# Edit backend/Dockerfile.prod, add:
# RUN apt-get update && apt-get install -y firefox-esr && rm -rf /var/lib/apt/lists/*

# Then rebuild:
docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/
docker stop opentranscribe-backend && docker rm opentranscribe-backend
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml up -d backend
```

### 6. Mount Firefox Profile

Add to `docker-compose.yml` under backend volumes:

```yaml
volumes:
  - ~/.mozilla/firefox:/home/appuser/.mozilla/firefox:ro
```

Then restart:

```bash
./opentr.sh restart-backend
```

### 7. Test Conservatively

- Start with ONE public video
- Wait 5-10 minutes between tests
- Gradually increase usage over 24-48 hours

---

## Configuration (.env File)

### Minimal Setup (Default Protection)

These defaults are **already enabled** - no changes needed:

```bash
# Playlist staggering (enabled by default)
YOUTUBE_PLAYLIST_STAGGER_ENABLED=true
YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS=5
YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS=30

# Pre-download jitter (enabled by default)
YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED=true
YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS=2
YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS=15

# Rate limiting (enabled by default)
YOUTUBE_USER_RATE_LIMIT_ENABLED=true
YOUTUBE_USER_RATE_LIMIT_PER_HOUR=50
YOUTUBE_USER_RATE_LIMIT_PER_DAY=500
```

### Add Cookie Authentication (Optional - For Sign-In Required Videos)

**Method A: Browser Extraction (If you have Firefox on Linux machine)**

```bash
YOUTUBE_COOKIE_BROWSER=firefox
```

**Method B: Manual Cookie File (For headless servers)**

```bash
YOUTUBE_COOKIE_FILE=/app/cookies.txt
```

See `docs/YOUTUBE_COOKIE_AUTH.md` for detailed setup instructions.

### User-Configurable Download Quality

Users can set their preferred download quality in Settings → Media → Download Quality. The setting is stored per-user and passed to yt-dlp at download time.

| Quality Setting | yt-dlp Format String | Notes |
|----------------|---------------------|-------|
| `best` (default) | `bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best` | Highest available resolution |
| `1080p` | `bestvideo[height<=1080][ext=mp4]+bestaudio/best[height<=1080]` | 1080p cap |
| `720p` | `bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]` | 720p cap |
| `480p` | `bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]` | 480p cap |
| `audio_only` | `bestaudio[ext=m4a]/bestaudio` | Audio track only, no video |

**Audio-only mode** is useful for podcast-style content where video is not needed — significantly reduces download size and storage usage in MinIO.

---

## Deno JS Runtime (v0.4.0)

The download worker container (`celery-download-worker`) includes the **Deno JavaScript runtime** as part of the v0.4.0 base image. Deno enables yt-dlp to extract YouTube's JavaScript-based player code natively without spawning a full browser, improving compatibility with YouTube's latest player updates.

**Why Deno is needed:**
- YouTube's player code is increasingly JavaScript-heavy and requires a real JS runtime to extract video URLs
- yt-dlp 2026 switched to a Deno-based JS extractor for better compatibility
- Without Deno, some videos fall back to older extraction paths that YouTube actively blocks

**You do not need to configure anything** — Deno is automatically used by yt-dlp when available in the container.

**Verifying Deno is available:**
```bash
docker exec opentranscribe-celery-download-worker deno --version
```

**yt-dlp 2026 Best Practices (already applied):**
- Client rotation: `android`, `mweb`, `web` fallback chain
- `--extractor-retries 3` for transient extraction failures
- Automatic format selection preferring H.264/MP4 for browser compatibility
- Anti-throttling: randomized request delays at the HTTP level
- Respects `--rate-limit` to avoid triggering YouTube's bandwidth detection

## What Each Feature Does

### Playlist Staggering

**Problem:** All playlist videos dispatch simultaneously → bot detection
**Solution:** Progressive delays (video 1: 0s, video 2: 5-10s, video 3: 10-20s, etc.)
**Impact:** 50-video playlist spreads over ~25 minutes
**How:** Celery `countdown` parameter - efficient, workers stay free during delay

### Pre-Download Jitter

**Problem:** Predictable timing patterns → bot detection
**Solution:** Random 2-15 second delay before each download starts
**Impact:** Irregular timing looks more human
**How:** Python `time.sleep()` inside task - randomness at execution time

### Rate Limiting

**Problem:** User downloads 500 videos in 10 minutes → bot detection
**Solution:** Enforce 50/hour, 500/day limits per user via Redis
**Impact:** Prevents abuse, tracks quota per user
**How:** Redis check in API endpoint before queueing Celery task

### Cookie Authentication

**Problem:** YouTube requires sign-in for age-restricted/members-only videos
**Solution:** Share your browser's YouTube login session with yt-dlp
**Impact:** Can download any video you can watch in browser
**How:** yt-dlp reads browser's cookie database directly (no password needed)

**Technical Details:** See "How the Anti-Bot Delays Work" section in `docs/YOUTUBE_COOKIE_AUTH.md` for complete explanation of Celery vs Python delays and performance impact.

---

## Testing

### Check Rate Limit Quota

```bash
curl http://localhost:5174/api/files/youtube/quota \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "hourly_remaining": 50,
  "daily_remaining": 500,
  "hourly_limit": 50,
  "daily_limit": 500
}
```

### View Playlist Stagger Logs

```bash
docker logs opentranscribe-celery-worker 2>&1 | grep "scheduled with"
```

Expected output:
```
Video 1/10: 'Title 1' scheduled (+0s)
Video 2/10: 'Title 2' scheduled (+7s)
Video 3/10: 'Title 3' scheduled (+15s)
...
```

### View Pre-Download Jitter Logs

```bash
docker logs opentranscribe-celery-worker 2>&1 | grep "jitter"
```

Expected output:
```
Applying pre-download jitter: 8.3s
Applying pre-download jitter: 12.1s
```

---

## Disabling Features (If Needed)

Add to `.env` to disable any feature:

```bash
# Disable staggering (all videos dispatch immediately)
YOUTUBE_PLAYLIST_STAGGER_ENABLED=false

# Disable jitter (no random delays)
YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED=false

# Disable rate limiting (unlimited downloads)
YOUTUBE_USER_RATE_LIMIT_ENABLED=false

# Disable cookies (no authentication)
YOUTUBE_COOKIE_BROWSER=
```

Then restart: `./opentr.sh restart-backend`

---

## Files Modified

**New Files (original implementation):**
- `backend/app/services/youtube_rate_limiter.py` - Rate limiting service
- `docs/YOUTUBE_COOKIE_AUTH.md` - Detailed cookie setup guide
- `docs/YOUTUBE_ANTI_BOT_QUICKSTART.md` - This file

**Modified Files:**
- `backend/app/core/config.py` - Added YouTube config settings, Deno path, quality settings
- `backend/app/services/media_download_service.py` - Added cookie support, Deno integration, quality selection
- `backend/app/tasks/youtube_processing.py` - Added staggering, jitter, yt-dlp 2026 client rotation
- `backend/app/api/endpoints/files/url_processing.py` - Added rate limiter
- `backend/Dockerfile.prod` - Added Deno runtime installation
- `.env.example` - Added YouTube configuration section

---

## Support

**Full Documentation:** See `docs/YOUTUBE_COOKIE_AUTH.md`
**Troubleshooting:** Check section at end of YOUTUBE_COOKIE_AUTH.md
**Recovery Guide:** See "Recovering from IP Ban" in YOUTUBE_COOKIE_AUTH.md
