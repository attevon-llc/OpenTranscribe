---
sidebar_position: 4
---

# Search & Filters

OpenTranscribe provides powerful hybrid search combining full-text and semantic search with advanced filtering.

![Search transcripts page with advanced filters sidebar](/img/screenshots/search/search-empty.png)

## The Search Page

Click **Transcript Search** in the top navigation bar to open the **Search Transcripts** page. Type a query and press Enter (or click the magnifier button); suggestions appear as you type, and choosing a title suggestion opens that file directly.

### Result sources

Once a query has run, a row of chips lets you choose where to search: **Title**, **Transcript**, **Summary** and **Speaker**. All four are on by default, each chip shows its match count, and you can turn any of them off. The chip counts show keyword matches only; in Smart mode additional related results can appear. If you turn every chip off, the page asks you to select at least one source. Transcript, title and speaker hits are listed under **Transcripts**, and summary hits under **Summaries** (each section can be collapsed).

### Smart and Exact modes

Above the results, a toggle switches between:

- **Smart** (default): semantic plus keyword search. Related passages that do not contain your exact words are marked **Related**.
- **Exact**: keyword-only search for exact matches.

A status indicator beside the toggle reads **Semantic on** or **Keyword only**. If it reads **Keyword only** in Smart mode, the neural model is not deployed on that server and the search is falling back to keywords (see [Neural Search Setup](../configuration/neural-search-setup.md)).

### Sorting

**Sort by** offers **Relevance** (default), **Upload Date**, **Completed Date**, **Filename**, **Duration** and **File Size**. Everything except Relevance can be ascending or descending.

### Result cards and Details

Each result is one file: its title, a line of date, duration and relevance, and snippets with the matching words highlighted. A file with several matches lists the first few and offers **Show N more matches**; **Collapse all** / **Expand all** above the list opens or closes every match list at once.

- The play button on a snippet previews the audio or video from that point in a floating player.
- **View Transcript** opens the transcript with the matches highlighted, and **View summary** does the same for a summary hit.
- **Details** (**Pin details** on wide screens) shows the file's speakers, tags, collections, size and the sources that matched. On a wide screen the details sit in a side panel that follows the card you hover or focus; pinning keeps it on one result. Clicking a tag or collection in the details narrows the results to it. **Open file** goes to the file page.

### Speaker operator

Type `speaker:"Name"` (for example `speaker:"Joe Rogan" china`) to search only within what that speaker said.

### Semantic search

Semantic search finds conceptually similar content even when the exact words differ, and works across paraphrases. In Smart mode it runs alongside keyword search and the two result lists are fused into one ranking. If the neural model is unavailable, search falls back to keywords automatically.

:::note[Hybrid Search Fix (v0.4.0)]
A critical bug in OpenSearch 3.4 caused hybrid search to silently fall back to keyword-only (BM25) search due to an `ArrayIndexOutOfBoundsException` triggered by the combination of aggregations, `collapse`, and RRF search pipelines. This was fixed in v0.4.0 — semantic search now works as intended. The improvement is significant: for example, searching "pytorch" went from 1 result to 83 results after the fix. Typo tolerance and query latency (255–500ms) are also confirmed working. If you were using an earlier version, reindexing is not required — the fix is in the query layer.
:::

## Filters

The filter sidebar (on the Search page and in the gallery, where it can be collapsed and reopened with **Show filters panel**) narrows results by:
- **File Type**: audio vs video
- **Speakers**: files with a specific speaker (named speakers are listed individually; speakers nobody has named yet are grouped under **Unlabeled speakers**)
- **Duration**: file length
- **Date Range**: upload date
- **Processing Status**
- **Tags**: custom labels
- **Collections**: grouped media files
- **Language**: shown when your library has more than one language
- **Ownership** (gallery only): **All Files**, **My Files** or **Shared with Me**, plus an owner picker

**Reset** clears every filter. In the gallery, the filename and title search box is in the toolbar above the file list rather than in the sidebar.

## Gallery Views

![Gallery overview with filter sidebar showing tags, collections, and speakers](/img/screenshots/gallery/gallery-overview.png)

The main file gallery supports multiple display modes:

- **Grid View**: Card-based layout showing thumbnails and metadata (default)
- **List View**: Compact table layout for faster scanning of large libraries
- **Sorting**: the **Sort by** menu offers Upload Date, Completed Date, Filename, Duration and File Size
- **Infinite scroll**: more files load as you scroll, and a count shows how many of the matching files are loaded
- **Virtual Scrolling**: Smooth performance even with thousands of files

## Search Query Syntax

### Keyword Search Behavior

Keyword search is a plain-language search; it does not parse Boolean operators. Words such as `AND`, `OR` and `NOT`, and wildcard characters such as `*`, are not special and are treated as ordinary text.

| Syntax | Example | Description |
|--------|---------|-------------|
| **Several words** | `project deadline` | Matches transcripts containing the words, ranking passages where they appear close together or in order higher |
| **Exact phrase** | `"project deadline"` | Matches the exact phrase, with no typo tolerance |
| **Typo tolerance** | Automatic | Handles typos and minor spelling variations (unquoted queries only) |
| **Speaker** | `speaker:"Joe Rogan" china` | Searches only that speaker's words |

### Semantic Search Tips

Semantic search finds conceptually related content even when exact words differ:

- Search for `"budget concerns"` to find mentions of "financial constraints", "cost overruns", or "spending limits"
- Search for `"next steps"` to find action items, follow-ups, and task assignments
- Works best with natural language queries rather than single keywords
- Semantic search runs automatically alongside keyword search in Smart mode

### Combining Filters with Search

For maximum precision, combine text queries with filters:

1. Enter your search query in the search bar
2. Use the filter sidebar (**Show filters panel** if it is collapsed)
3. Apply filters (speakers, date range, tags, collections, file type, and so on)
4. Results are narrowed to match both the query and all active filters; a **Filters applied to results** note confirms they are active

### Search Result Highlighting

When search results are returned, the matching text is highlighted in the result snippets, so you can see exactly where your search terms appear within each transcript. Words found only by semantic similarity are highlighted differently from exact keyword matches.

![Search results with highlighted matching transcript segments](/img/screenshots/search/search-results.png)

## Finding Text Inside a Transcript

The gallery/library search above searches across your whole collection. A separate **find bar**
searches *within* the transcript or summary you currently have open, and shares its component
and fuzzy-matching behavior (diacritic- and case-folding) everywhere it appears, so it behaves
the same way in the transcript viewer as it does in the summary panel.

In the transcript viewer, the find bar highlights matches in the currently loaded portion of the
transcript instantly, then resolves a lightweight, debounced count query scoped to that file to
show an accurate **N of M+** indicator and progressively load more matches if any exist outside
what's currently rendered — so a match late in a long transcript is never missed just because
that page hasn't loaded yet. In the summary panel, where the whole summary is already in memory,
the find bar is a simpler, complete-from-the-start version of the same component.

### Searching Settings

**Settings** has its own macOS-style search: a search box above the sidebar tabs that replaces
the grouped navigation with ranked, highlighted results as you type. Selecting a result jumps
straight to that section and briefly flashes the matched control, so you don't have to remember
which tab a setting lives under. It works across all supported languages and only surfaces
settings your account and edition can actually see.

## Transcript Comments

Add timestamped comments to any transcript for notes, annotations, or collaboration.

### Adding a Comment

1. Open a transcript and navigate to the **Comments** section
2. Type your comment in the text area
3. Click **"Mark Current Time"** to attach the current playback timestamp -- a timestamp is required
4. Click **"Add Comment"** to save

### Timestamp-Linked Comments

Every comment is linked to a specific point in the media file:

- Comments display the timestamp in `MM:SS` format
- Click the **timestamp badge** on any comment to jump the player to that moment
- Comments are sorted chronologically by timestamp
- You can clear and re-set the timestamp before submitting

### Editing and Deleting Comments

- **Edit**: Click the **Edit** link on your own comment, modify the text, then click **Save**
- **Delete**: Click the **Delete** link on your own comment and confirm the deletion
- Only the comment author can edit or delete their comments
- A confirmation dialog appears before deletion to prevent accidental removal

### Comments in Exports

When exporting a transcript as `.txt`, you can optionally include comments in the export output (see [Export Options](./uploading-files.md#export-options)).

## Next Steps

- [Collections](./collections.md)
- [Speaker Management](./speaker-management.md)
