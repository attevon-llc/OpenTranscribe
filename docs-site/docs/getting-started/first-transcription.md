---
sidebar_position: 3
title: Your First Transcription
---

# Your First Transcription

This guide walks you through creating your first transcription in OpenTranscribe, from upload to analysis.

## Step 1: Prepare Your Media File

OpenTranscribe supports a wide range of formats:

### Audio Formats
- **MP3** - Most common audio format
- **WAV** - Uncompressed audio (best quality)
- **FLAC** - Lossless compression
- **M4A** - Apple audio format
- **OGG** - Open-source audio format

### Video Formats
- **MP4** - Most common video format
- **MOV** - Apple video format
- **AVI** - Windows video format
- **MKV** - Matroska video container
- **WEBM** - Web-optimized video

### File Size Limits
- Maximum file size: **4GB**
- Recommended: Under 2GB for faster processing
- Long videos (3+ hours) supported

:::tip Best Results
For best transcription quality:
- Use **clear audio** with minimal background noise
- **Single speaker per channel** if possible
- **Good microphone** quality (not phone speaker recordings)
- **Volume normalized** - not too quiet or clipping
:::

## Step 2: Upload Your File

### Via Web Interface

1. **Click "Upload Files"** button in the top navigation bar
2. **Drag and drop** your file onto the upload zone, or **click to browse**
3. **Select your file** from your computer
4. Watch the upload progress in the **floating upload manager**

![Drag and drop upload interface](/img/screenshots/upload/upload-file-drop.png)

### Via URL (YouTube)

OpenTranscribe can download and process YouTube videos:

1. **Click "Upload from URL"** in the navbar
2. **Paste the YouTube URL** (supports playlists too!)
3. Click "**Download and Process**"
4. The video will be downloaded and queued for transcription

![URL import - paste YouTube or video URL](/img/screenshots/upload/upload-url.png)

### Via Recording

Record audio directly in your browser:

1. **Click the microphone icon** in the navbar
2. **Select your microphone** device
3. **Click "Start Recording"**
4. **Monitor audio levels** to ensure good volume
5. **Pause/Resume** as needed
6. **Click "Stop"** when finished
7. The recording is **automatically uploaded** and processed

![Upload settings - configure tags, speakers, and model](/img/screenshots/upload/upload-settings-step.png)

## Step 3: Monitor Processing

OpenTranscribe processes files through 13 stages:

### Processing Stages

```mermaid
flowchart LR
    A[Queued] --> B[Preprocessing]
    B --> C[Transcribing]
    C --> D[Diarizing]
    D --> E[Post-Processing]
    E --> F[Complete]

    style A fill:#6b7280
    style B fill:#3b82f6
    style C fill:#8b5cf6
    style D fill:#ec4899
    style E fill:#f59e0b
    style F fill:#10b981
```

1. **Queued** - File is waiting in the processing queue
2. **Starting** - Worker is beginning processing
3. **Extracting Audio** - Converting video to audio if needed
4. **Loading Models** - Loading WhisperX and PyAnnote models
5. **Transcribing** - AI transcription in progress
6. **Aligning** - Word-level timestamp alignment
7. **Diarizing** - Detecting and separating speakers
8. **Creating Profiles** - Generating voice fingerprints
9. **Matching Speakers** - Cross-video speaker matching
10. **Generating Waveform** - Creating audio visualization
11. **Indexing** - Adding to search index
12. **Saving** - Storing results to database
13. **Complete** - Ready to view!

### Where to Watch Progress

**Real-Time Updates:**
- **Upload Manager** (bottom-right floating panel)
- **Notifications Panel** (bell icon in navbar)
- **File Library** (processing badge on file cards)
- **Flower Dashboard** (http://localhost:5175/flower)

![Notifications panel showing transcription progress](/img/screenshots/workflow/notifications-multiple.png)

**Processing Time Estimates:**

| Duration | GPU (RTX 3080) | CPU (8-core) |
|----------|----------------|--------------|
| 5 min    | ~30 seconds    | ~5 minutes   |
| 30 min   | ~3 minutes     | ~30 minutes  |
| 1 hour   | ~5 minutes     | ~60 minutes  |
| 3 hours  | ~15 minutes    | ~3 hours     |

:::info Processing Speed
With GPU acceleration and the `large-v3-turbo` model (default), OpenTranscribe processes at **~40x realtime speed** (full pipeline). A 1-hour file transcribes in about 5 minutes!
:::

## Step 4: View Your Transcript

Once processing completes, click on the file to view the transcript.

### Transcript Features

![File detail page with video player and synchronized transcript](/img/screenshots/transcript/file-detail-video.png)

**Interactive Transcript:**
- **Click any word** to jump to that moment in the audio
- **Speaker labels** automatically assigned (SPEAKER_00, SPEAKER_01, etc.)
- **Timestamps** show when each speaker talks
- **Word-level highlighting** follows audio playback

**Waveform Player:**
- **Click anywhere** on the waveform to seek to that time
- **Visual representation** of audio amplitude
- **Zoom controls** for detailed view
- **Speaker segments** color-coded

**Playback Controls:**
- **Play/Pause** audio playback
- **Speed control** (0.5x to 2x speed)
- **Volume control**
- **Keyboard shortcuts** (Space to play/pause, arrow keys to seek)

## Step 5: Edit Speaker Names

OpenTranscribe automatically detects speakers but labels them generically. You can edit names:

### Edit Speaker Names

1. **Click the "Edit Speakers" button** below the transcript
2. **Click on a speaker label** (e.g., "SPEAKER_00")
3. **Type the actual name** (e.g., "John Smith")
4. **Press Enter** or click outside to save
5. The name **updates throughout the transcript** instantly

![Speaker editor with identified speakers and voice match suggestions](/img/screenshots/speakers/speaker-editor-panel.png)

### Create Speaker Profiles

When you name a speaker, OpenTranscribe can:

1. **Create a global profile** for that speaker
2. **Generate a voice fingerprint** using their audio
3. **Suggest that speaker** in future transcriptions
4. **Track their appearances** across multiple files

See [Speaker Management](../user-guide/speaker-management.md) for advanced features.

## Step 6: Generate a Summary (Optional)

If you've configured an LLM provider, you can generate AI summaries:

### Configure LLM (One-Time)

1. Go to **User Settings** (gear icon)
2. Click **"LLM Configuration"** tab
3. Select a **provider** (OpenAI, Claude, vLLM, Ollama)
4. Enter your **API key** or endpoint
5. **Test the connection**
6. Click **"Save"**

### Generate Summary

1. **Open a transcription**
2. Click the **"Summarize" button** at the top
3. Choose a **summary prompt**:
   - **BLUF (Bottom Line Up Front)** - Executive summary format
   - **Meeting Notes** - Action items and decisions
   - **Custom prompts** - Create your own!
4. Watch the **progress** (takes 10-60 seconds depending on length)
5. **View the summary** in the Summary tab

### Summary Features

The default BLUF summary includes:

- **Overview** - High-level summary in 2-3 sentences
- **Key Points** - Bullet points of main topics discussed
- **Action Items** - Tasks and assignments with priorities
- **Decisions Made** - Key decisions and outcomes
- **Follow-up Items** - Things to revisit or research
- **Speaker Analysis** - Who spoke most, key contributions

![AI-generated BLUF summary with executive overview and action items](/img/screenshots/ai-features/ai-summary.png)

## Step 7: Explore Advanced Features

### Search Your Transcript

**Keyword Search:**
```
Search for: "project deadline"
```
Finds exact matches of that phrase

**Semantic Search:**
```
Search for: "budget concerns"
```
Finds related concepts like "financial constraints", "cost overruns", etc.

### Add Comments

1. **Click anywhere** in the transcript
2. **Type your comment** in the comment field
3. **Press Enter** to save
4. Comments are **timestamped** and linked to that moment

### Export Options

**Export Formats:**
- **TXT** - Plain text transcript (configurable: timestamps, speaker names, comments)
- **JSON** - Structured data with timestamps and speaker info
- **CSV** - Comma-separated values for spreadsheet analysis
- **SRT** - SubRip subtitle file for video players
- **VTT** - WebVTT subtitle format for web playback

**Export Methods:**
1. Click the **"Export"** dropdown on the transcript page
2. Choose your **format**
3. The file downloads immediately

![Export format options dropdown](/img/screenshots/transcript/export-options.png)

See [Uploading Files - Export Options](../user-guide/uploading-files.md#export-options) for full details on each format.

### Organize with Collections

Group related files:

1. Click the **"Collections"** button
2. **Create a new collection** (e.g., "Q1 2024 Meetings")
3. **Add files** by clicking the collection tag
4. **Filter** your library by collection

## Performance Tips

### For Faster Processing

1. **Enable GPU** acceleration if available
2. **Use smaller models** for quick drafts (base or medium)
3. **Process overnight** for large batches
4. **Multi-GPU scaling** for high-throughput needs

### For Better Accuracy

1. **Use large-v3 model** for best transcription quality (including non-English and translation tasks)
2. **Good audio quality** - clear, well-recorded audio
3. **Edit speaker names** to improve future speaker matching
4. **Verify and correct** any transcription errors

## Troubleshooting

### Upload Fails

- **Check file size** (must be under 4GB)
- **Check format** (must be supported audio/video)
- **Check disk space** (need enough storage)
- **Try again** - network errors can be temporary

### Processing Stuck

- **Check logs**: `./opentranscribe.sh logs celery-worker`
- **Check Flower**: http://localhost:5175/flower
- **Restart workers**: `./opentranscribe.sh restart`
- **Check GPU memory**: `nvidia-smi`

### Poor Transcription Quality

- **Improve audio** - re-record with better microphone
- **Reduce background noise** - use noise cancellation
- **Larger model** - switch to `large-v3` for better accuracy
- **Language setting** - ensure correct language selected

### Speakers Not Detected

- **Check HuggingFace token** - required for diarization
- **Clear audio** - speakers need distinct voices
- **Adjust MIN/MAX speakers** in configuration
- **Manual editing** - edit speaker labels manually if needed

## Next Steps

Now that you've created your first transcription, explore:

- **[Speaker Management](../user-guide/speaker-management.md)** - Advanced speaker features
- **[AI Summarization](../user-guide/ai-summarization.md)** - Generate insights from transcripts
- **[Search & Filters](../user-guide/search-and-filters.md)** - Find content across all files
- **[Collections](../user-guide/collections.md)** - Organize your media library

## Need Help?

- **[FAQ](../faq.md)** - Common questions and answers
- **[GitHub Issues](https://github.com/attevon-llc/OpenTranscribe/issues)** - Report bugs or request features
- **[GitHub Discussions](https://github.com/attevon-llc/OpenTranscribe/discussions)** - Ask questions and share tips

Happy transcribing!
