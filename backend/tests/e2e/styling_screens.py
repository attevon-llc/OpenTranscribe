"""Screen manifest for the styling A/B tool (``styling_ab.py``).

Pure data plus a validator, so ``tests/unit/test_styling_ab.py`` can import it without a
browser. Screen ids are STABLE: the lane cards in
``docs/design/frontend_styling_layer_plan.md`` refer to them by name.

Steps vocabulary (see ``styling_ab.run_step``): ``click:<css>``, ``hover:<css>``,
``focus:<css>``, ``wait:<css>``, ``press:<key>``, ``settings:<section-id>``, ``tab:<id>``
(``#tab-<id>``, the id ``ui/Tabs`` renders), ``scroll:<css>``, ``patch:<url regex>|<json fields>``
(merge fields into the real GET answer, see ``styling_ab._patch_route``) and ``reload:page``.

Paths may contain ``{file_uuid}``, resolved at run time to the newest completed file.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

THEMES = ("light", "dark")
LOCALES = ("en", "ar")
#: ``<locale>-<theme>``. All four always run: captures are cheap.
VARIANTS = tuple(f"{loc}-{theme}" for loc in LOCALES for theme in THEMES)

STEP_KINDS = frozenset(
    {"click", "hover", "focus", "wait", "press", "settings", "tab", "scroll", "patch", "reload"}
)

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
    notes: str = field(default="", compare=False)


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
        ready=".gallery-action-buttons",
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

#: ``settings:<id>`` ids are the ``SettingsSection`` ids in ``components/SettingsModal.svelte``;
#: ``tab:<id>`` ids are the real ``Tabs`` ids read from the section components
#: (``lib/settings/*Tabs.ts``, ``AuthenticationSettings.svelte``). Password policy lives
#: inside the ``local`` authentication tab (there is no tab of its own).
_SETTINGS = (
    *(
        _settings(s)
        for s in (
            "profile",
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
        "/files/{file_uuid}?t=1",
        steps=("wait:.transcript-segment.active-segment",),
        ready=".transcript-segment",
        notes="the ?t= deep link seeks and marks the segment under the playhead",
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

SCREENS: tuple[Screen, ...] = (*_LOGGED_OUT, *_APP, *_SETTINGS, *_FILE_DETAIL_STATES)

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
        if s.skip_reason is not None and not s.skip_reason.strip():
            problems.append(f"{s.id}: empty skip_reason")
    return problems
