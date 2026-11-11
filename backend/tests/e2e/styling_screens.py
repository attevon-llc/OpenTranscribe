"""Screen manifest for the styling A/B tool (``styling_ab.py``).

Pure data plus a validator, so ``tests/unit/test_styling_ab.py`` can import it without a
browser. Screen ids are STABLE: the lane cards in
``docs/design/frontend_styling_layer_plan.md`` refer to them by name.

Steps vocabulary (see ``styling_ab.run_step``): ``click:<css>``, ``hover:<css>``,
``focus:<css>``, ``wait:<css>``, ``press:<key>``, ``settings:<section-id>``, ``tab:<id>``
(``#tab-<id>``, the id ``ui/Tabs`` renders), ``scroll:<css>``, ``patch:<url regex>|<json fields>``
(merge fields into the real GET answer, see ``styling_ab._patch_route``) and ``reload:page`` and
``addclass:<css>|<class>`` (put a class the app sets from live playback on an element, so its rule renders)
and ``fixture:<url regex>|<file>`` (answer every matching GET with a whole JSON file from
``styling_fixtures/``, for a state the stack has no data for at all; see ``fixture_problem``).

Paths may contain ``{file_uuid}``, resolved at run time to the newest completed file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

THEMES = ("light", "dark")
LOCALES = ("en", "ar")
#: ``<locale>-<theme>``. All four always run: captures are cheap.
VARIANTS = tuple(f"{loc}-{theme}" for loc in LOCALES for theme in THEMES)

STEP_KINDS = frozenset(
    {
        "click",
        "hover",
        "focus",
        "wait",
        "press",
        "settings",
        "tab",
        "scroll",
        "patch",
        "reload",
        "addclass",
        "local",
        "file",
        "route",
        "pause",
        "fixture",
    }
)

#: Whole JSON answers for ``fixture:`` steps. Each file copies the shape of the endpoint's real
#: serializer (named in the screen that uses it), so the page renders what the backend would send.
FIXTURE_DIR = Path(__file__).resolve().parent / "styling_fixtures"


def load_fixture(name: str) -> object:
    """The parsed JSON of ``styling_fixtures/<name>``."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture_problem(arg: str) -> str | None:
    """Why ``fixture:<arg>`` cannot run, or ``None``.

    The argument is ``<url regex>|<file>``: a regex that compiles, and a bare ``.json`` file name
    (no directory part) that exists in ``FIXTURE_DIR`` and parses.
    """
    pattern, sep, name = arg.rpartition("|")
    if not sep or not pattern or not name:
        return "needs <url regex>|<file>"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"url regex {pattern!r} does not compile ({exc})"
    if Path(name).name != name or not name.endswith(".json"):
        return f"{name!r} must be a bare .json file name in styling_fixtures/"
    try:
        load_fixture(name)
    except FileNotFoundError:
        return f"no fixture file styling_fixtures/{name}"
    except ValueError as exc:
        return f"styling_fixtures/{name} is not valid JSON ({exc})"
    return None


#: Masked on every screen: the navbar bell renders an unread-count pill that exists in one
#: notification state and not the other (same reasoning as test_visual_regression.py).
# The audio player's slider track holds the buffered-range bar, whose width tracks how much of the
# file the browser has fetched (network timing, not styling). Masking the buffer element alone
# leaves its differently sized mask rectangle in the diff, so the whole track is masked.
# .free-space prints the host's live free disk bytes: machine state, not styling.
GLOBAL_MASKS = (
    ".notifications-btn",
    ".toast-container",
    "media-slider-track",
    ".free-space",
)
# The reconnect banner shows only while the websocket is reconnecting, so it can be present on one
# side and absent on the other. A mask would paint a rectangle on one side only; hide it instead.
TRANSIENT_HIDE_CSS = ".connection-banner{display:none!important}"


@dataclass(frozen=True)
class Screen:
    """One capturable screen. ``skip_reason`` records a screen the stack cannot show."""

    id: str
    path: str
    auth: bool = True
    steps: tuple[str, ...] = ()
    masks: tuple[str, ...] = ()
    full_page: bool = True
    skip_reason: str | None = None
    #: Wait for this selector after navigation (and before the steps) so a half-rendered
    #: page is never photographed.
    ready: str | None = None
    #: Per-screen viewport (width, height); ``None`` keeps the context default.
    viewport: tuple[int, int] | None = None
    #: localStorage seeds applied before the app loads and removed again afterwards, because
    #: the browser context is shared by every later screen of the same variant.
    storage: tuple[tuple[str, str], ...] = ()
    #: False for a screen that holds a request open on purpose (``route:...|0``), which would
    #: keep ``networkidle`` from ever firing.
    network_idle: bool = True
    #: True for a screen that queues an upload: the queue persists in the browser profile, and a
    #: shared context would restore it (badge and all) into the next screen captured.
    isolated: bool = False
    notes: str = field(default="", compare=False)


#: The gallery has finished its first load: real content (``GalleryGrid``'s readiness oracle, the
#: one ``conftest.py``'s ``gallery_page`` fixture waits for), or its empty or error state. Its
#: skeleton swapping for cards behind an open modal changes pixels, so a screen that opens a
#: modal over the gallery waits for this first.
GALLERY_READY = (
    '[data-testid="gallery-files-loaded"], .content-area .empty-state, .content-area .error-state'
)


def _settings(section: str, *extra: str, masks: tuple[str, ...] = ()) -> Screen:
    """A settings section, optionally followed by more steps (usually ``tab:<id>``)."""
    suffix = "-".join(s.split(":", 1)[1] for s in extra) if extra else ""
    sid = f"settings-{section}" + (f"-{suffix}" if suffix else "")
    return Screen(
        id=sid,
        path="/",
        steps=(f"settings:{section}", *extra),
        masks=(".count-chip", ".meta-line", *masks),
        # The modal is a fixed-position overlay: a viewport capture is what a user sees.
        full_page=False,
        ready=GALLERY_READY,
    )


_LOGGED_OUT = tuple(
    Screen(id=sid, path=path, auth=False, ready="main, form, .auth-container, body")
    for sid, path in (
        ("login", "/login"),
        ("register", "/register"),
        ("forgot-password", "/forgot-password"),
        ("reset-password", "/reset-password?token=invalid"),
        ("verify-email", "/verify-email?token=invalid"),
        ("accept-invite", "/accept-invite?token=invalid"),
    )
)

_GALLERY_MASKS = (".count-chip", ".meta-line")

_UPLOAD_OPEN = ("click:.upload-btn", "wait:.tab-button")
_UPLOAD_FILE = (*_UPLOAD_OPEN, "file:.drop-zone input[type=file]", "wait:.file-name")
#: Submit with the review-with-defaults shortcut; the mocked route decides what the queue shows.
_UPLOAD_SUBMIT = ("click:.nav-next", "click:.nav-review-defaults", "click:.nav-submit")
_UPLOAD_PREPARE = "route:**/api/files/prepare|"

_APP = (
    Screen("gallery", "/", masks=_GALLERY_MASKS, ready=".gallery-action-buttons"),
    Screen(
        "gallery-select",
        "/",
        steps=("wait:.file-card, .file-list-row", "click:.select-btn"),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
    ),
    Screen(
        "gallery-filters",
        "/",
        steps=("click:.filter-toggle-btn",),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
    ),
    Screen(
        "upload",
        "/",
        steps=("click:.upload-btn", "wait:.tab-button"),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".upload-btn",
    ),
    Screen(
        "upload-tags",
        "/",
        # Remembered tags from the last submit restore as chips on the Tags step.
        steps=(
            'local:opentr:uploadPreviousValues={"tagNames":["interview","q4-review"],'
            '"collectionIds":[],"skippedSteps":[],"timestamp":1}',
            *_UPLOAD_FILE,
            "click:.nav-next",
            "wait:.selected-chips .chip",
            # The wizard auto-focuses a different control per run; pin the focus ring here.
            "focus:.create-input",
        ),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".upload-btn",
        notes="chip + previous-values banner, both themes",
    ),
    Screen(
        "upload-record",
        "/",
        steps=(*_UPLOAD_OPEN, "click:.tab-button >> nth=2", "wait:.start-button, .unsupported-msg"),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".upload-btn",
        notes="idle recorder only: no microphone in the capture browser, so the level bars and "
        "paused dot are not reachable",
    ),
    Screen(
        "upload-failed",
        "/",
        steps=(
            _UPLOAD_PREPARE + "500",
            *_UPLOAD_FILE,
            *_UPLOAD_SUBMIT,
            # 3 retries with 1-2 s, 2-4 s and 4-8 s backoff: after 16 s the row stays failed.
            "pause:16000",
            "wait:.error-message",
        ),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".upload-btn",
        isolated=True,
        notes="UploadManager panel with a failed row and its error message",
    ),
    Screen(
        "upload-active",
        "/",
        steps=(
            # The tray stays minimised (and shows its activity dot) when the visitor collapsed it
            # before; the store reads that flag once at load, hence the reload.
            "local:upload-tray-user-collapsed=1",
            "reload:page",
            "wait:.upload-btn",
            _UPLOAD_PREPARE + "0",
            *_UPLOAD_FILE,
            *_UPLOAD_SUBMIT,
            "wait:.activity-indicator",
        ),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".upload-btn",
        network_idle=False,
        isolated=True,
        notes="minimised badge with the activity dot while a request is held in flight",
    ),
    Screen("file-detail", "/files/{file_uuid}", ready=".transcript-segment"),
    Screen(
        "file-detail-editor",
        "/files/{file_uuid}",
        steps=("click:.edit-speakers-button", "wait:.speaker-editor-container"),
        ready=".transcript-segment",
    ),
    Screen(
        "file-detail-comments",
        "/files/{file_uuid}",
        steps=("scroll:.comments-wrapper",),
        ready=".transcript-segment",
    ),
    Screen(
        "search",
        "/search?q=the",
        ready=".search-page, main",
        # "N results in <time> ms": the server's search time changes the text width per run.
        masks=(".result-summary",),
    ),
    Screen(
        "chat",
        "/chat",
        ready='[data-testid="chat-composer-input"]',
        skip_reason=(
            "needs an LLM provider (start the stack with --with-mock-llm) and one canned "
            "exchange; enable by removing this reason once a provider is configured"
        ),
    ),
    Screen(
        "speakers-clusters",
        "/speakers?tab=clusters",
        masks=(".last-clustered-chip", ".member-count", ".speakers-page .badge"),
        ready=".speakers-page",
    ),
    Screen(
        "speakers-profiles",
        "/speakers?tab=profiles",
        masks=(".speakers-page .badge",),
        ready=".speakers-page",
    ),
    Screen(
        "speakers-inbox",
        "/speakers?tab=inbox",
        masks=(".speakers-page .badge",),
        ready=".speakers-page",
    ),
    Screen("file-status", "/file-status", ready=".file-status-page"),
    Screen(
        "notifications",
        "/",
        steps=("click:.notifications-btn",),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".notifications-btn",
        notes="the bell itself is masked everywhere; the open panel is what is compared",
    ),
    Screen(
        "user-menu",
        "/",
        steps=("click:.user-button", "wait:.dropdown-menu"),
        masks=(*_GALLERY_MASKS, ".dropdown-header strong"),
        full_page=False,
        ready=".user-button",
    ),
    Screen(
        "about",
        "/",
        steps=("click:.navbar-brand .logo-link", "wait:.modal-title"),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".navbar-brand .logo-link",
    ),
    # Ends on the OPEN dialog. The tool never clicks confirm: e2e must not mutate data.
    Screen(
        "confirm-delete",
        "/",
        steps=(
            "wait:.file-card, .file-list-row",
            "click:.select-btn",
            "click:.file-card, .file-list-row",
            "click:.delete-btn",
            "wait:[role=dialog], [role=alertdialog]",
        ),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".gallery-action-buttons",
    ),
    Screen("ui-kit", "/a11y-fixtures/ui-kit", ready='[data-kit-section="buttons"]'),
    Screen(
        "ui-kit-focus",
        "/a11y-fixtures/ui-kit",
        steps=(
            "focus:[data-kit-section='buttons'] .btn-primary",
            "focus:[data-kit-section='toggles'] [role='switch']",
        ),
        ready='[data-kit-section="buttons"]',
        notes="keyboard focus ring over the first button and the first switch",
    ),
    # --- S20B D4 proof: focus ONLY a .btn-primary (ui-kit-focus ends on the switch, whose
    # ring already used --focus-ring-color). Dark outline: rgb(37,99,235) -> rgb(96,165,250).
    Screen(
        "ui-kit-btn-focus",
        "/a11y-fixtures/ui-kit",
        steps=("focus:[data-kit-section='buttons'] .btn-primary",),
        ready='[data-kit-section="buttons"]',
        notes="keyboard focus ring on the first primary button only (D4 evidence)",
    ),
    # --- end S20B D4 block
    Screen(
        "ui-kit-confirm",
        "/a11y-fixtures/ui-kit",
        steps=(
            "click:[data-kit-trigger='confirm-danger']",
            "wait:[role=dialog], [role=alertdialog]",
        ),
        full_page=False,
        ready='[data-kit-section="buttons"]',
        notes="the open confirm dialog; never confirmed",
    ),
    Screen("status-badges", "/a11y-fixtures/status-badges", ready=".status-badge"),
)

# --- BEGIN block: navbar + gallery states (styling row 3.2) ---------------------------------
# Unreachable deterministically, so NOT captured: the Navbar recording indicator and popup
# (need a live MediaRecorder / microphone grant; recordingStore has no seed hook) and the
# VirtualGrid new-file glow (a 0.6s animation fired by a websocket upload event, and the tool
# runs with reduced motion, which switches the animation off).
_S32_MOBILE = (390, 844)
_S32_LIST = (("gallery-view-mode", "list"),)

_NAVBAR_GALLERY = (
    Screen(
        "gallery-sort-open",
        "/",
        steps=("click:.sort-dropdown .sort-button", "wait:body > .dropdown-menu"),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".gallery-action-buttons",
        notes="SortDropdown menu open: solid surface, border and shadow (portaled to body)",
    ),
    Screen(
        "gallery-list",
        "/",
        storage=_S32_LIST,
        steps=("wait:.file-list-row",),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
        notes="list view (VirtualList), no hover",
    ),
    Screen(
        "gallery-list-hover",
        "/",
        storage=_S32_LIST,
        steps=("wait:.file-list-row", "hover:.file-list-row"),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
        notes="list view with the first row hovered",
    ),
    Screen(
        "gallery-selected",
        "/",
        steps=(
            "wait:.file-card, .file-list-row",
            "click:.select-btn",
            "click:.file-card >> nth=0",
            "click:.file-card >> nth=1",
            "wait:.file-card.selected",
        ),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
        notes="grid view, two files selected (selected border)",
    ),
    Screen(
        "gallery-list-selected",
        "/",
        storage=_S32_LIST,
        steps=(
            "wait:.file-list-row",
            "click:.select-btn",
            "click:.file-list-row >> nth=0",
            "click:.file-list-row >> nth=1",
            "wait:.file-list-row.selected",
        ),
        masks=_GALLERY_MASKS,
        ready=".gallery-action-buttons",
        notes="list view, two rows selected",
    ),
    Screen(
        "gallery-mobile-nav",
        "/",
        viewport=_S32_MOBILE,
        steps=("click:.mobile-toggle button", "wait:.mobile-overlay"),
        masks=_GALLERY_MASKS,
        full_page=False,
        ready=".mobile-toggle button",
        notes="390x844 with the nav menu open: shows the --modal-backdrop overlay",
    ),
)
# --- END block: navbar + gallery states (styling row 3.2) -----------------------------------

#: ``settings:<id>`` ids are the ``SettingsSection`` ids in ``components/SettingsModal.svelte``;
#: ``tab:<id>`` ids are the real ``Tabs`` ids read from the section components
#: (``lib/settings/*Tabs.ts``, ``AuthenticationSettings.svelte``). Password policy lives
#: inside the ``local`` authentication tab (there is no tab of its own).
_SETTINGS = (
    # The active-sessions table gains a row for every capture's own login, and its auto-layout
    # column widths move with that content (3,000+ computed width diffs between two captures of
    # one server): machine state, like .free-space, so it is masked.
    _settings("profile", masks=(".sessions-table",)),
    *(
        _settings(s)
        for s in (
            "groups",
            "processing-pipeline",
            "transcription",
            "speaker-identification",
            "chat",
            "content-redaction",
            "llm-provider",
            "ai-prompts",
            "organization-context",
            "auto-labeling",
            "download",
            "media-sources",
            "watch-sources",
            "recording",
            "audio-extraction",
            "admin-users",
            "authentication",
            "audit-logs",
            "quarantine",
            "takedown-notices",
            "file-status",
            "legal",
            "data-management",
        )
    ),
    _settings("system-statistics", masks=(".settings-modal .stats-grid",)),
    *(
        _settings("authentication", f"tab:{t}")
        for t in (
            "local",
            "ldap",
            "oidc",
            "pki",
            "proxy",
            "saml",
            "session",
        )
    ),
    *(
        _settings("data-management", f"tab:{t}")
        for t in (
            "data-retention",
            "data-backup",
            "data-integrity",
            "data-search",
            "data-embeddings",
        )
    ),
    *(_settings("transcription", f"tab:{t}") for t in ("tx-provider", "tx-vocabulary")),
    *(_settings("speaker-identification", f"tab:{t}") for t in ("spk-attributes", "spk-engine")),
)

# --- S34 transcript states (lane s34: rendering of changes the base screens never reached) ------
_FILE_DETAIL_STATES = (
    # Patched-data states: a state the seeded data lacks is added by patching the real answer
    # (``patch`` + ``reload``).
    Screen(
        "file-detail-shared",
        "/files/{file_uuid}",
        steps=(
            r'patch:/api/files/[0-9a-f-]{36}(\?.*)?$|{"my_permission": "editor"}',
            "reload:page",
            "wait:.shared-chip",
        ),
        ready=".transcript-segment",
        notes="the file header's shared-with-me chip; the seeded file is owned by the capture user",
    ),
    Screen(
        "file-detail-speaker-chips",
        "/files/{file_uuid}",
        steps=(
            r'patch:/api/speakers\?.*file_uuid=|{"show_profile_badge": true, "show_suggestions_section": true, '
            r'"metadata_hints": [{"name": "Alex Rivera", "role": "host", "confidence": 0.8, "source": "title"}], '
            r'"profile_suggestions": [{"name": "Jordan Lee", "confidence": 0.9, "confidence_percentage": "90%", '
            r'"reason": "voice match"}]}',
            "reload:page",
            "click:.edit-speakers-button",
            "wait:.speaker-editor-container",
            "click:.suggestions-toggle",
            "wait:.profile-chip",
        ),
        ready=".transcript-segment",
        notes="profile badge, metadata chip and profile chip on every speaker",
    ),
    Screen(
        "file-detail-export-open",
        "/files/{file_uuid}",
        steps=("click:.export-transcript-button", "wait:.export-dropdown.open"),
        ready=".transcript-segment",
        notes="TranscriptActionsBar export dropdown (--shadow-lg)",
    ),
    Screen(
        "file-detail-active-segment",
        "/files/{file_uuid}",
        steps=("addclass:.transcript-segment|active-segment",),
        ready=".transcript-segment",
        notes="playback marks the playing segment with this class; the ?t= deep link cleared it again "
        "before capture (timing), so the class is added to render the rule",
    ),
    Screen(
        "file-detail-speaker-dropdown",
        "/files/{file_uuid}",
        steps=(
            "click:.transcript-segment .speaker-trigger",
            "wait:.speaker-dropdown-portal .dropdown-menu",
        ),
        ready=".transcript-segment",
        # The menu closes on window resize, and a full-page capture fires one: viewport only.
        full_page=False,
        notes="SegmentSpeakerDropdown's body-portal menu open on the first segment",
    ),
    Screen(
        "file-detail-edit-hover",
        "/files/{file_uuid}",
        steps=("hover:.transcript-segment .edit-button",),
        ready=".transcript-segment",
        notes="segment Edit button hover (underline appears, colour kept)",
    ),
    Screen(
        "search-floating-preview",
        "/search?q=the",
        steps=(
            "click:.matches-toggle",
            "click:.occurrence-row .preview-btn",
            "wait:.sticky-preview",
        ),
        ready=".search-page, main",
        masks=(".result-summary", ".preview-time", ".preview-player-container"),
        full_page=False,
        notes="FloatingPreviewPlayer shadow",
    ),
)

# --- speakers states (styling row 3.1) -------------------------------------------------------
# The seeded stack has no speaker clusters, shared profiles or inbox suggestions, so `.cluster-card`,
# `.shared-badge` and `.confidence` never render from its data. These screens answer the list
# endpoints with whole fixture files instead of mutating the stack. Each file copies the shape of
# its serializer (the endpoints declare `response_model=dict[str, Any]`, so the serializer IS the
# schema): `speaker_clusters_page.json` = services/clustering/queries.py `list_clusters`,
# `speaker_cluster_detail.json` = `get_cluster_detail` (gender_composition from
# clustering/gender.py `compute_gender_composition`), `speaker_inbox_page.json` =
# clustering/verification.py `get_unverified_speakers`, `speaker_profiles.json` =
# api/endpoints/speaker_profiles.py `_profile_list_entry`. The three quality scores and the three
# inbox confidences fall in the success / warning / error bands of SpeakerClusterCard and
# SpeakerInboxItem; one profile is shared by another user and one has an image avatar.
_SPEAKER_MASKS = (".last-clustered-chip", ".member-count", ".speakers-page .badge")
_CLUSTERS_FIXTURES = (
    r"fixture:/api/speaker-clusters\?|speaker_clusters_page.json",
    r"fixture:/api/speaker-clusters/[0-9a-f-]{36}$|speaker_cluster_detail.json",
)
_PROFILES_FIXTURE = r"fixture:/api/speaker-profiles/profiles(\?.*)?$|speaker_profiles.json"
_INBOX_FIXTURE = r"fixture:/api/speaker-clusters/unverified/inbox|speaker_inbox_page.json"
_CLUSTER_EXPANDED = (
    *_CLUSTERS_FIXTURES,
    "reload:page",
    "click:.cluster-card .card-header",
    "wait:.cluster-card .card-actions",
    "wait:.member-row",
)

_SPEAKER_STATES = (
    Screen(
        "speakers-clusters-expanded",
        "/speakers?tab=clusters",
        steps=_CLUSTER_EXPANDED,
        masks=_SPEAKER_MASKS,
        ready=".speakers-page",
        notes="first card expanded: promote/merge/split/unassign/delete outlines; quality badge in "
        "the success, warning and error text colours on the three cards",
    ),
    Screen(
        "speakers-clusters-unassign-hover",
        "/speakers?tab=clusters",
        steps=(*_CLUSTER_EXPANDED, "hover:.action-btn.unassign"),
        masks=_SPEAKER_MASKS,
        ready=".speakers-page",
        notes="the Unassign button hovered: amber fill with the dark --text-on-warning label",
    ),
    Screen(
        "speakers-profiles-shared",
        "/speakers?tab=profiles",
        steps=(_PROFILES_FIXTURE, "reload:page", "wait:.shared-badge"),
        masks=_SPEAKER_MASKS,
        ready=".speakers-page",
        notes="a profile shared by another user carries the Shared badge (both themes)",
    ),
    Screen(
        "speakers-inbox-confidence",
        "/speakers?tab=inbox",
        steps=(_INBOX_FIXTURE, "reload:page", "wait:.inbox-item .confidence"),
        masks=_SPEAKER_MASKS,
        ready=".speakers-page",
        notes="three suggestions whose confidence text is in the success, warning and error bands",
    ),
)

# --- search-hit highlights and the media scrim (plan 3.2, "Media, highlights and washes") ------
# Transcript find types into the toolbar's SearchBar one key at a time (`press:`), so the
# highlights are the app's own for that query: "th" marks words in the text (the first match is
# `.current`), "speaker" marks the segment speaker chips (#837 ring). The search screens query a
# word of the seeded sample ("fragments"): the generic "the" of `search` returns only semantic
# hits, which carry neither a `<mark>` nor a keyword mark.
_FIND_OPEN = (
    "click:.search-trigger-button",
    "wait:.transcript-search .search-bar-input",
    "click:.transcript-search .search-bar-input",
)

_HIGHLIGHT_STATES = (
    Screen(
        "file-detail-find-text",
        "/files/{file_uuid}",
        steps=(*_FIND_OPEN, "press:t", "press:h", "wait:.transcript-search-highlight.current"),
        ready=".transcript-segment",
        notes="transcript find: word marks and the current mark (--highlight-search)",
    ),
    Screen(
        "file-detail-find-speaker",
        "/files/{file_uuid}",
        steps=(
            *_FIND_OPEN,
            *(f"press:{c}" for c in "speaker"),
            "wait:.segment-speaker.current-match",
        ),
        ready=".transcript-segment",
        notes="transcript find on a speaker name: the chip rings (--highlight-search/-current)",
    ),
    Screen(
        "search-marks",
        "/search?q=fragments",
        steps=("wait:.search-page mark",),
        ready=".search-page, main",
        masks=(".result-summary",),
        notes="result snippets with <mark> hits (--highlight-bg)",
    ),
    Screen(
        "search-transcript-view",
        "/search?q=fragments",
        steps=("click:.view-transcript-btn", "wait:.search-keyword-match"),
        ready=".search-page, main",
        masks=(".result-summary",),
        full_page=False,
        notes="the search result's transcript view: keyword marks",
    ),
    Screen(
        "speakers-profiles-avatar-hover",
        "/speakers?tab=profiles",
        steps=(
            _PROFILES_FIXTURE,
            "reload:page",
            "wait:.profile-avatar-wrapper img",
            "hover:.profile-avatar-wrapper",
        ),
        masks=_SPEAKER_MASKS,
        ready=".speakers-page",
        notes="the camera overlay on a profile picture (--media-scrim)",
    ),
)

SCREENS: tuple[Screen, ...] = (
    *_LOGGED_OUT,
    *_APP,
    *_NAVBAR_GALLERY,
    *_SETTINGS,
    *_FILE_DETAIL_STATES,
    *_SPEAKER_STATES,
    *_HIGHLIGHT_STATES,
)

#: Screens the lane cards use as "control" (must pass the zero rule in intended mode).
DEFAULT_CONTROLS = ("gallery", "file-detail", "login", "settings-profile")


def by_id() -> dict[str, Screen]:
    """Index the manifest by screen id."""
    return {s.id: s for s in SCREENS}


def validate(screens: tuple[Screen, ...] = SCREENS) -> list[str]:
    """Return a list of manifest problems (empty means well-formed)."""
    problems: list[str] = []
    seen: set[str] = set()
    for s in screens:
        if s.id in seen:
            problems.append(f"duplicate screen id {s.id!r}")
        seen.add(s.id)
        if not s.id or s.id != s.id.lower() or " " in s.id or "/" in s.id:
            problems.append(f"screen id {s.id!r} must be lowercase, no spaces or slashes")
        if not s.path.startswith("/"):
            problems.append(f"{s.id}: path {s.path!r} must start with '/'")
        for step in s.steps:
            kind, sep, arg = step.partition(":")
            if kind not in STEP_KINDS or not sep or not arg:
                problems.append(f"{s.id}: malformed step {step!r}")
            elif kind == "fixture" and (why := fixture_problem(arg)):
                problems.append(f"{s.id}: malformed step {step!r}: {why}")
        if s.viewport is not None and not all(n > 0 for n in s.viewport):
            problems.append(f"{s.id}: viewport {s.viewport!r} must be positive")
        if s.skip_reason is not None and not s.skip_reason.strip():
            problems.append(f"{s.id}: empty skip_reason")
    return problems
