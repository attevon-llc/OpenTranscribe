# YouTube Cookie Authentication & Ban Recovery

This guide helps you enable cookie authentication for YouTube downloads and recover from IP bans.

## Quick Start

### 1. Add to `.env` file

```bash
# Enable cookie authentication (choose ONE method):

# Method A: Browser extraction (requires Firefox in container)
YOUTUBE_COOKIE_BROWSER=firefox

# Method B: Manual cookie file (for headless servers)
YOUTUBE_COOKIE_FILE=/app/cookies.txt
```

### 2. Restart backend

```bash
./opentr.sh restart-backend
```

That's it! Your downloads will now use your YouTube login session.

---

## Why Cookie Authentication?

**Without cookies:** Can only download public videos
**With cookies:** Can download sign-in required, age-restricted, and members-only videos

**Anti-Bot Benefit:** Cookies make your downloads look like normal browser traffic, reducing bot detection risk.

---

## ⚠️ CRITICAL: Account Ban vs IP Ban

### Two Types of Bans

**IP Ban (what you likely have):**
- Your machine's IP address is flagged
- Affects ALL activity from that IP
- Browser shows "Are you a bot?" challenge
- Temporary - usually clears after IP change + 24-48 hours

**Account Ban (much worse!):**
- Your Google account gets suspended
- Permanent - affects all Google services
- Can happen if you use cookies from a banned IP

### The Risk of Cookies on a Banned IP

**DANGER:** If you use cookies from your Google account while your IP is banned, YouTube will associate your legitimate account with bot behavior and could ban your ACCOUNT permanently.

**Safe approach:**
1. Enable VPN → Get new IP
2. Clear all browser cookies
3. Wait 24-48 hours
4. THEN log into YouTube fresh (with new IP)
5. THEN set up cookie extraction

**Never use cookies from the banned IP period!**

---

## Do You Even Need Cookies?

**Most users don't!** Ask yourself:

❌ **Public videos only?** → Don't need cookies (90% of YouTube)
✅ **Age-restricted videos?** → Need cookies
✅ **Members-only content?** → Need cookies
✅ **Private/unlisted videos?** → Need cookies

**Recommendation:** Run WITHOUT cookies for 24-48 hours first. The anti-bot features (staggering, jitter, rate limits) work perfectly without cookies and keep your Google account safe.

---

## Setup Methods

### Method A: Browser Extraction (Recommended for XRDP/GUI)

**Requirements:**
- Firefox installed in Docker container
- Browser profile directory accessible to container
- You logged into YouTube in that browser

**Step 1: Add Firefox to backend container**

Edit `backend/Dockerfile.prod`, add after other apt-get installs:

```dockerfile
RUN apt-get update && \
    apt-get install -y firefox-esr && \
    rm -rf /var/lib/apt/lists/*
```

**Step 2: Mount your Firefox profile**

Edit `docker-compose.yml`, add under backend service volumes:

```yaml
volumes:
  - ~/.mozilla/firefox:/home/appuser/.mozilla/firefox:ro
```

**Step 3: Enable in `.env`**

```bash
YOUTUBE_COOKIE_BROWSER=firefox
```

**Step 4: Rebuild and restart**

```bash
docker build -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/
./opentr.sh restart-backend
```

**Step 5: Log into YouTube**

Open Firefox on your Linux machine and log into YouTube. The cookies are automatically shared with the container.

---

### Method B: Manual Cookie File (Headless Servers)

**Best for:** Servers without GUI/XRDP

**Step 1: Export cookies from your desktop browser**

1. Install browser extension "Get cookies.txt LOCALLY" (Firefox/Chrome)
2. Visit youtube.com while logged in
3. Click extension to export `cookies.txt`
4. Copy file to server

**Step 2: Mount cookies.txt to container**

Edit `docker-compose.yml`, add under backend service volumes:

```yaml
volumes:
  - /path/to/cookies.txt:/app/cookies.txt:ro
```

**Step 3: Enable in `.env`**

```bash
YOUTUBE_COOKIE_FILE=/app/cookies.txt
```

**Step 4: Restart**

```bash
./opentr.sh restart-backend
```

---

## About yt-dlp Detection (Honest Assessment)

### Can YouTube Detect yt-dlp?

**Yes, technically.** YouTube can detect yt-dlp through:
- User-Agent patterns
- Missing browser fingerprints (WebGL, Canvas)
- Request timing patterns
- Known yt-dlp signatures

### What We're Already Doing (v0.4.0)

OpenTranscribe v0.4.0 applies yt-dlp 2026 best practices:

```python
"extractor_args": {
    "youtube": {
        "player_client": ["android", "mweb", "web"],  # Rotates clients with mweb fallback
        "player_skip": ["webpage", "configs"],  # Avoids extra requests
    }
},
"http_headers": {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)...",
    "Accept": "text/html,application/xhtml+xml,...",
},
"extractor_retries": 3,       # Retry on transient extraction errors
"fragment_retries": 10,       # Retry on fragment download errors
```

**Deno JS Runtime:** The download worker container includes the Deno JavaScript runtime. yt-dlp uses Deno to execute YouTube's JavaScript player code for URL extraction — this significantly improves compatibility with YouTube's latest player updates and is more reliable than the older Python-based extraction approach.

**This makes yt-dlp look like Android YouTube app traffic, not web scraping.**

### What The Anti-Bot Features Add

- **Irregular timing** (jitter) - Defeats pattern recognition algorithms
- **Human-like pacing** (staggering) - Mimics browsing behavior
- **Reasonable volume** (rate limits) - Prevents bulk scraping detection
- **Deno JS extraction** - Native JavaScript execution for YouTube player code

**Together:** You look like "person using yt-dlp for personal archiving" - which YouTube generally tolerates.

### Risk Levels

**Low Risk:** Single videos, small playlists (<10), spread over time
**Medium Risk:** Large playlists (50+), even with staggering
**High Risk:** 500 videos/day every day, even with all protections

**The reality:** YouTube tolerates yt-dlp for personal use. They mainly ban:
1. Commercial scrapers (terabytes of data)
2. Automated services (thousands of IPs)
3. Obvious bot patterns (simultaneous downloads)

Your new setup moves you from category #3 to "reasonable personal use."

---

## Recovering from IP Ban

### Signs You're Banned

- Browser shows "Are you a bot?" challenge
- Downloads fail with "sign in to confirm you're not a bot" error
- Happens even with different YouTube accounts

### ⚠️ CRITICAL: Clear Celery Queue First!

**BEFORE starting containers**, you must purge old failed tasks:

```bash
# Start Redis only
docker compose up -d redis

# Purge ALL queued tasks (old download attempts)
docker run --rm --network opentranscribe_default \
  -e REDIS_HOST=redis \
  -e REDIS_PORT=6379 \
  davidamacey/opentranscribe-backend:latest \
  celery -A app.core.celery purge -f

# Alternative: Use redis-cli to clear everything
docker exec -it opentranscribe-redis redis-cli FLUSHDB
```

**Why this matters:** Celery will auto-retry old failed downloads when backend starts. This will immediately trigger bot detection again!

### Recovery Steps (MUST DO IN ORDER!)

#### 1. Enable VPN (CRITICAL!)

```bash
# Enable PIA VPN or any VPN service
# Verify new IP:
curl ifconfig.me
```

**DO NOT proceed without VPN!** Testing without VPN reinforces the ban.

#### 2. Clear Browser Cookies

- Firefox: Settings → Privacy → Clear Data → Cookies
- Chrome: Settings → Privacy → Clear Browsing Data → Cookies

This removes "tainted" cookies associated with the banned IP.

#### 3. Restart Browser with VPN Active

Open browser, verify YouTube doesn't show bot challenge. If it does, cycle VPN to a new IP.

#### 4. Log into YouTube Fresh

Log in with VPN active. These are "clean" cookies tied to the new IP.

#### 5. Set Up Cookie Authentication

Follow Method A or B above using the fresh cookies.

#### 6. Test Conservatively

**Start with anti-bot features but NO cookies:**

```bash
# Keep these in .env (default - already enabled):
YOUTUBE_PLAYLIST_STAGGER_ENABLED=true
YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED=true
YOUTUBE_USER_RATE_LIMIT_ENABLED=true

# Leave cookies disabled initially:
YOUTUBE_COOKIE_BROWSER=
YOUTUBE_COOKIE_FILE=
```

**Testing schedule:**

```bash
# Hour 0: Restart backend to pick up the .env changes above
./opentr.sh restart-backend

# Hour 0: Test ONE public video
# Submit single video, wait 15 minutes

# Hour 1: Test small playlist (2-3 videos)
# Check logs for staggering:
docker logs opentranscribe-celery-worker 2>&1 | grep "scheduled"

# Hour 2: 5 videos
# Hour 4: 10 videos
# Hour 8: 20 videos

# Day 2: Normal usage (50/hour, 500/day)
```

**Check logs during testing:**

```bash
# Verify jitter is working
docker logs opentranscribe-celery-worker 2>&1 | grep "jitter"
# Should see: "Applying pre-download jitter: X.Xs"

# Verify staggering is working
docker logs opentranscribe-celery-worker 2>&1 | grep "scheduled"
# Should see progressive delays

# Check for errors
docker logs opentranscribe-celery-worker 2>&1 | grep -i "error\|sign"
```

**If you see ANY errors: STOP immediately and wait 24 hours.**

#### 7. After 48 Hours: Consider Cookies (Optional)

If 48 hours pass with no errors AND you need sign-in required videos:

```bash
# Only NOW add cookies (with VPN still active)
YOUTUBE_COOKIE_BROWSER=firefox

# Your cookies are now tied to the clean VPN IP
```

---

## Configuration Reference

### Cookie Authentication

```bash
# Browser extraction (auto-detects cookies)
YOUTUBE_COOKIE_BROWSER=firefox
# Options: firefox, chrome, chromium, edge, safari, opera

# Explicit cookie file (manual export)
YOUTUBE_COOKIE_FILE=/app/cookies.txt
```

### Rate Limiting (Prevents Future Bans)

```bash
# Enable rate limiting
YOUTUBE_USER_RATE_LIMIT_ENABLED=true

# Quotas per user
YOUTUBE_USER_RATE_LIMIT_PER_HOUR=50    # Downloads per hour
YOUTUBE_USER_RATE_LIMIT_PER_DAY=500    # Downloads per day
```

### Playlist Staggering (Prevents Simultaneous Requests)

```bash
# Enable staggering
YOUTUBE_PLAYLIST_STAGGER_ENABLED=true

# Delay settings
YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS=5    # Min delay between videos
YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS=30   # Max delay (caps here)
YOUTUBE_PLAYLIST_STAGGER_INCREMENT=5      # Delay increases per video
```

### Pre-Download Jitter (Adds Randomness)

```bash
# Enable jitter
YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED=true

# Random delay range
YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS=2
YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS=15
```

---

## Best Practices Going Forward

### Conservative Usage Patterns

**Good patterns (low risk):**
- Single videos throughout the day
- Small playlists (<10 videos) with default staggering
- Total: 20-30 videos/day spread over 8+ hours

**Moderate patterns (medium risk):**
- Medium playlists (10-30 videos) with staggering
- Total: 50-100 videos/day spread over full day

**Aggressive patterns (high risk):**
- Large playlists (50+ videos) back-to-back
- Total: 500 videos/day at rate limit max
- May work, but increases detection risk

### If Errors Occur

**First error:**
- STOP all downloads immediately
- Wait 1 hour
- Test with single video

**Second error within 24 hours:**
- STOP completely
- Wait 24-48 hours
- May need to cycle VPN IP

**Third error:**
- Consider IP/account is flagged
- Switch VPN server/region
- Wait 48 hours minimum
- Consider reducing limits by 50%

---

## Troubleshooting

### "Containers auto-retry old failed tasks!"

**Problem:** Celery recovers failed tasks when backend starts, immediately triggering bot detection again.

**Solution:** Purge queue BEFORE starting:

```bash
# Start Redis only
docker compose up -d redis

# Purge ALL queued tasks
docker run --rm --network opentranscribe_default \
  -e REDIS_HOST=redis \
  davidamacey/opentranscribe-backend:latest \
  celery -A app.core.celery purge -f

# Or use redis-cli
docker exec -it opentranscribe-redis redis-cli FLUSHDB

# NOW restart backend
./opentr.sh restart-backend
```

### "Still getting sign-in errors after enabling cookies"

**Check 1:** Is Firefox installed in the container?

```bash
docker exec opentranscribe-backend firefox --version
```

**Check 2:** Is the profile directory mounted?

```bash
docker exec opentranscribe-backend ls -la /home/appuser/.mozilla/firefox/
```

**Check 3:** Are you using VPN consistently?

Make sure VPN was active when you logged into browser AND when testing downloads.

**Check 4:** Check logs for cookie extraction

```bash
docker logs opentranscribe-celery-worker 2>&1 | grep -i cookie
```

### "Videos work individually but playlists trigger bans"

**Solution:** Increase stagger delays temporarily

```bash
# Add to .env:
YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS=30
YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS=120

# Test with small playlists (2-3 videos) first
```

### "Cookies expire or stop working"

**Cause:** YouTube sessions expire after ~2 weeks of inactivity

**Solution:** Log into YouTube in browser again to refresh cookies. No config changes needed.

---

## How the Anti-Bot Delays Work (Technical Details)

### Overview: Celery vs Python Delays

The anti-bot system uses **two different delay mechanisms** for different purposes:

1. **Playlist Staggering** → Celery countdown (efficient)
2. **Pre-Download Jitter** → Python time.sleep() (unpredictable)
3. **Rate Limiting** → Redis checks (before queueing)

### 1. Playlist Staggering - Celery Handles This ✅

```python
# When dispatching playlist videos:
process_youtube_url_task.apply_async(
    args=[url, user_id, file_uuid],
    countdown=15  # ← Celery waits 15 seconds before starting task
)
```

**How it works:**
- Task is placed in Redis queue **immediately**
- Celery scheduler holds it for `countdown` seconds
- Worker picks it up only after countdown expires
- **Worker is free** during countdown (can process other tasks)

**For a 3-video playlist:**
- Video 1: countdown=0s (starts immediately)
- Video 2: countdown=7s (Celery waits 7s, then worker picks up)
- Video 3: countdown=15s (Celery waits 15s, then worker picks up)

**Performance:** ✅ Efficient - workers stay free during countdown

---

### 2. Pre-Download Jitter - Python time.sleep() ⚠️

```python
# Inside the task, after it starts running:
import time
jitter_seconds = random.uniform(2, 15)
time.sleep(jitter_seconds)  # ← Python blocks the worker thread
```

**How it works:**
- Task **already running** on worker
- Python sleeps for 2-15 seconds (blocks that worker slot)
- **Worker is busy** during sleep (can't take other tasks)
- Then proceeds with download

**Why not use Celery countdown for jitter?**
- ❌ Countdown is set when dispatching (predictable pattern)
- ✅ Sleep inside task = randomness at execution time (unpredictable)
- ✅ Delay not visible in queue metadata (better anti-bot)

**Performance:** ⚠️ Blocks 1 worker slot for 2-15s, but download worker has concurrency=3, so minimal impact

---

### 3. Rate Limiting - Redis Checks (Before Celery)

```python
# In API endpoint, BEFORE dispatching to Celery:
allowed, reason = youtube_rate_limiter.check_rate_limit(user_id)
if not allowed:
    raise HTTPException(429, detail=reason)  # Reject before queueing

# If allowed, record download and dispatch:
youtube_rate_limiter.record_download(user_id)
process_youtube_url_task.delay(url, user_id, file_uuid)
```

**How it works:**
- API checks Redis quota **before** creating Celery task
- If over limit: HTTP 429 error, no task created at all
- If OK: Records download in Redis, then dispatches task to Celery

**Performance:** ✅ Very fast (Redis lookup ~1ms)

---

### Complete Flow

```
User submits video
      ↓
[API] Check rate limit (Redis) ← Blocks if over quota (429 error)
      ↓
[API] Record download in Redis
      ↓
[API] Dispatch task to Celery (countdown=0 for single video, 5-30s for playlist)
      ↓
[Celery Queue] Task waits for `countdown` seconds (worker is FREE)
      ↓
[Worker] Picks up task after countdown expires
      ↓
[Worker] Task runs: time.sleep(random 2-15s jitter) ← Worker BUSY
      ↓
[Worker] Download starts
      ↓
[Worker] Video processes
      ↓
Done
```

---

### Why Two-Layer Delays?

**Playlist Staggering (Celery countdown):**
- Purpose: Spread videos over time (long delays, 5-30s between videos)
- Benefit: Workers stay free, efficient resource usage
- Result: 10-video playlist spreads over ~5 minutes

**Pre-Download Jitter (Python sleep):**
- Purpose: Add randomness to each download (short delays, 2-15s unpredictable)
- Benefit: Timing is truly random at execution time
- Result: Each download has unique, unpredictable start time

**Combined Effect:**
- Playlist videos dispatch progressively (human pacing)
- Each download starts at random moment (human behavior)
- Pattern looks natural, not robotic

---

### Performance Impact

**Download Worker Configuration:**
- Concurrency: 3 parallel tasks
- Queue: `download`

**Impact of Jitter:**
- Blocks 1 of 3 worker slots for 2-15 seconds
- Other 2 slots remain free for parallel downloads
- Download itself takes 30s-5min, so jitter (2-15s) is <10% overhead
- **Minimal performance impact for significant anti-bot benefit**

---

## Security Notes

- **Your Google password is NEVER stored** - only session cookies are used
- Cookies are read-only from browser database files
- Container only has read access to your browser profile
- Cookies are only used for yt-dlp downloads, not stored anywhere else

---

## Disabling Cookie Authentication

To go back to non-authenticated downloads:

```bash
# Remove or comment out in .env:
# YOUTUBE_COOKIE_BROWSER=
# YOUTUBE_COOKIE_FILE=

# Restart backend
./opentr.sh restart-backend
```

Anti-bot features (staggering, jitter, rate limits) still work without cookies.
