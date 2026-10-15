# UX/UI designer review notes (2026-09)

Cleaned-up version of the raw dictated notes from the UX designer's review
(source: `ux-ui-eng-notes.md`, dictated; speech-to-text errors corrected, wording preserved where
meaning was clear). Items marked **(?)** were ambiguous in the dictation and need confirming
against the designer's mockups. Companion notes: `~/ux-notes/750_filter_panel_ux_notes.md` and
issue #750 (filter panel), label `ux-review-2026-09`.

## Docs site

1. Add a one-click **copy** icon next to the copy-URL item.
2. Give the OpenTranscribe-specific column a lighter border, and verify the check marks are correct.
3. Replace the one-line intro at the top with a meaningful blurb: "If you are doing X, you will
   finish in N steps." Professional and human in tone.

## Tags / collections pickers

4. Tag and collection search + multi-select must look like the search bars at the top and match
   the left sidebar's search and multi-select (same style, same display).

## Upload modal

5. Fixed size, fixed distance from top, left and right, centered, so stepping through the stages
   does not resize or flicker. Scroll must not chain: scrolling inside the modal must not scroll
   the page underneath.
6. **Bug:** "Create collection" is greyed out/disabled. It must work and reuse the same create
   modal as the gallery.
7. Fit content to width: tags are much narrower than collections, so make the tags table narrower
   and centered and the collections table wider.
8. Remove "Skip" where "Next" is present (same action).
9. Step 6 is labelled **Review**; the bottom button must say Review as well, and the Skip-to
   button must match.
10. Transcript quality/speed dropdown: add helper text in brackets or parentheses describing
    speed versus quality.
11. AI summary defaults **on** (blue toggle on); the user may switch it off. The current skip
    wording is inverted.
12. On upload, auto-show a progress panel at the bottom right (like Google Drive), collapsible.
    Raise the contrast of the active-count colour (a bolder colour).

## Notifications panel

13. Show completed-ago ("5 minutes ago") and, bottom right, a "completed in Nm Ns" tag, in the
    spirit of the LLM "thought for N seconds" label.
14. Icons must match their routes: topic icon = tag icon; collections and tags icons match; the
    transcription notification uses the OpenTranscribe logo (voice to document).
15. Notifications could auto-clear where appropriate. No requirement yet.

## Navigation and gallery list

16. Title nav bar: slightly darker, lifted, more in line with the mainstream UI the designer
    supplied.
17. List mode: title too bold. Recommended weight 500, title case, left aligned; numeric columns
    (duration, size, counts) right aligned; icons vertically centered; about 32px more padding
    between the speaker count and the duration. The toolbar's item count on the right is to be
    redesigned (separate item, see 40).
18. All list columns use the same font size; only the title is slightly bolder. (The header row
    must match too.)
19. Sort dropdown: the direction caret must be the same arrow as the Sort button icon: down
    arrow for descending, up arrow for ascending.
20. Status: replace the hover pop-up text with a green circle + check or a red circle + X, in
    both grid and list, with a real tooltip on hover ("Completed", "Error").

## Filter panel (left sidebar)

21. **Collapse control:** the button on the left is smaller than its icon. Use a left chevron to
    collapse; when collapsed, the control is a right chevron to open. The arrow changes
    direction; the button does **not** swap sides.
22. File name/title search shows duplicate helper text (inside and below). Remove the one below.
23. Section headers less bold (the "Search" header, then the quick chips).
24. More vertical space between sections: larger dividers, more padding above and below.
25. **Bug (important):** "No speakers detected" cannot be filtered for.
26. **Order of filters matches the list column order** (type, title, speakers, duration, date,
    size, status, plus tags/collections).
27. **Dates:** no keyboard entry. Someone looking for 2011-2015 while in 2026 cannot correct the
    date. Replace with a start/end input that accepts typing and also offers a graphical picker.
28. **File type chips** get relevant icons (audio, video, and documents, which is a future
    feature; add it to the tickets).
29. **Duration filter bug:** the length filter does not apply automatically. Recommended: quick
    chips above the slider, 1-10 min, 11-30, 31-60, 60+, with the slider below. The designer
    also suggested **removing the file size filter**, because size is a proxy for duration and
    few people filter by file size.
30. **Ownership** uses the same searchable dropdown as tags and collections, listing owners of
    items I can access.

## Search page

31. **Bug:** clearing the search (circle-X or backspacing to empty) does not clear results. It
    must clear and show "no search results yet". (Caching the last query across page changes is
    fine.)
32. Rename "Chat" to **AI Chat** (process dropdown, with the count, and the navbar) so it is not
    mistaken for chatting with people.
33. Navbar order: Gallery, Transcript Search, AI Chat, Speakers (Documents later). "Transcript
    Search" must not stack on two lines; keep it horizontal.
34. **Transcript modal opened from a search hit:** the arrow keys, counts and search must be
    sticky at the top (currently they scroll away after auto-scrolling to the hit). It should be
    the same component as the file-detail transcript search. Show counts per mode, such as
    "Keyword (n)" and "Semantic (n)", with the arrows to step through, sticky.

## File detail page

35. The scroll-position bar at the right of the transcript, used to jump to the player, is
    confusing. Replace with a **"Jump to current"** button above the transcript, on the same row
    as search. Search expands only to the size of that button; the button has a hover title.
36. **Bug:** unconfirmed speakers show the LLM-suggested name instead of the speaker number; the
    long suggestion overlaps and is hidden, and the speaker icon is the wrong size.
37. **Bug:** "Add speaker" in a transcript segment does nothing. It needs a modal/inline input
    for a new name.
38. Navbar file-status icon should be one that says status.
39. Reorganise the left column: player, waveform, then Export / Edit speakers / Download buttons,
    then expandable Tags, Collections, Analytics, File details (nothing below). Comments move to
    the right column below the transcript and are titled "Comments" (not "Comments and notes").
    Edit speakers replaces the Tags/Collections/Analytics/File details area while editing.
    The file detail header at the top goes away.
40. **Gallery toolbar redesign.** Row 1: filename/title search left-justified; Tags and
    Collections buttons then Add media, right-justified (Add media rightmost). Row 2: the item
    count chip, then "Select items" (expands to Process/Organize/Delete); right-justified: sort
    dropdown then the grid/list toggle. This removes the sidebar's file-name search.
41. **Bug:** the two icon buttons at the right of the file-detail search (clear / next) are not
    light/dark compatible and are invisible. Run an audit of every button in light and dark
    (use Playwright).

## Other

42. Settings left sidebar: audit and group the most-used items, titled properly.
43. Speaker management: add a small banner (as on search) saying clustering and metrics are
    still being developed.
44. Speaker profiles: show "Male"/"Female" (or M/F) next to the gender icon, following best practice.
45. Speaker "Inbox" should be labelled **Review** with a count so it does not read as email.

## Open (?)

- Item 17: "500 Bold" read as weight 500; confirm.
- Item 29: file-size removal is the designer's suggestion, not a decision.
