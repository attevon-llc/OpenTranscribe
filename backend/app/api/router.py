import logging

from fastapi import APIRouter

from . import websockets
from .endpoints import admin
from .endpoints import admin_timing
from .endpoints import asr_settings
from .endpoints import auth
from .endpoints import auth_config
from .endpoints import backup_settings
from .endpoints import chat
from .endpoints import combined_speaker_migration
from .endpoints import comments
from .endpoints import custom_vocabulary
from .endpoints import embedding_migration
from .endpoints import engine_settings
from .endpoints import groups
from .endpoints import llm_settings
from .endpoints import llm_status
from .endpoints import media_collections
from .endpoints import media_mirror_settings
from .endpoints import org_admin
from .endpoints import prompts
from .endpoints import redaction_settings
from .endpoints import search
from .endpoints import speaker_attribute_migration
from .endpoints import speaker_clusters
from .endpoints import speaker_profiles
from .endpoints import speakers
from .endpoints import summarization
from .endpoints import system
from .endpoints import tags
from .endpoints import tasks
from .endpoints import topics
from .endpoints import transcript_segments
from .endpoints import user_files
from .endpoints import user_settings
from .endpoints import users
from .endpoints import watch_sources
from .endpoints.files import router as files_router
from .endpoints.files.management import router as file_management_router

logger = logging.getLogger(__name__)

api_router = APIRouter()


# Function to include routers with proper route handling for consistent frontend-backend communication
def include_router_with_consistency(router, prefix, tags=None, capability=None):
    """Include a router with consistent route handling that works both with and without trailing slashes

    This ensures consistent API behavior regardless of whether the frontend sends requests
    with or without trailing slashes, which is important for production with nginx.

    Args:
        router: The router to include
        prefix: The prefix for the router (e.g., '/users')
        tags: Tags for the router for API documentation
        capability: Optional capability key gating the whole router — routes
            return 404 when the deployment's capability resolver disables it
            (community default: everything enabled, so no behavior change;
            platform staff bypass applies). See app.core.capabilities.
    """
    if tags is None:
        tags = [prefix.strip("/")]  # Default tag based on prefix

    # Ensure prefix starts with / but doesn't end with one
    normalized_prefix = "/" + prefix.strip("/")

    dependencies = None
    if capability is not None:
        from fastapi import Depends

        from app.core.capabilities import require_capability

        dependencies = [Depends(require_capability(capability))]

    # Include the router with the normalized prefix
    api_router.include_router(
        router, prefix=normalized_prefix, tags=tags, dependencies=dependencies
    )


# Include routers from different endpoints with consistent path handling
include_router_with_consistency(auth.router, prefix="/auth", tags=["auth"])
include_router_with_consistency(files_router, prefix="/files", tags=["files"])
include_router_with_consistency(file_management_router, prefix="/files", tags=["file-management"])
include_router_with_consistency(search.router, prefix="/search", tags=["search"])
include_router_with_consistency(speakers.router, prefix="/speakers", tags=["speakers"])
include_router_with_consistency(
    speaker_profiles.router, prefix="/speaker-profiles", tags=["speaker-profiles"]
)
include_router_with_consistency(
    speaker_clusters.router, prefix="/speaker-clusters", tags=["speaker-clusters"]
)
include_router_with_consistency(comments.router, prefix="/comments", tags=["comments"])
include_router_with_consistency(tags.router, prefix="/tags", tags=["tags"])
include_router_with_consistency(users.router, prefix="/users", tags=["users"])
include_router_with_consistency(
    watch_sources.router,
    prefix="/watch-sources",
    tags=["watch-sources"],
    capability="watch_sources",
)
include_router_with_consistency(tasks.router, prefix="/tasks", tags=["tasks"])
include_router_with_consistency(admin.router, prefix="/admin", tags=["admin"])
include_router_with_consistency(admin_timing.router, prefix="/admin", tags=["admin-timing"])
# Org-admin tenant administration (audit-log read + GDPR erasure). Gated by the
# "organizations" capability: 404 in the community edition (no orgs), enabled by
# the cloud capability resolver. require_org_admin further gates each route.
include_router_with_consistency(
    org_admin.router,
    prefix="/org-admin",
    tags=["org-admin"],
    capability="organizations",
)
include_router_with_consistency(
    auth_config.router,
    prefix="/admin/auth-config",
    tags=["auth-config"],
    capability="auth.config_ui",
)
include_router_with_consistency(system.router, prefix="/system", tags=["system"])
include_router_with_consistency(
    media_collections.router, prefix="/collections", tags=["collections"]
)
include_router_with_consistency(groups.router, prefix="/groups", tags=["groups"])
include_router_with_consistency(user_files.router, prefix="/my-files", tags=["user-files"])
include_router_with_consistency(summarization.router, prefix="/files", tags=["summarization"])
include_router_with_consistency(prompts.router, prefix="/prompts", tags=["prompts"])
include_router_with_consistency(
    llm_settings.router,
    prefix="/llm-settings",
    tags=["llm-settings"],
    capability="llm.user_settings",
)
include_router_with_consistency(llm_status.router, prefix="/llm", tags=["llm-status"])
include_router_with_consistency(
    asr_settings.router,
    prefix="/asr-settings",
    tags=["asr-settings"],
    capability="asr.user_providers",
)
include_router_with_consistency(
    engine_settings.router,
    prefix="/admin/engine-settings",
    tags=["admin"],
    capability="engine.settings",
)
include_router_with_consistency(
    custom_vocabulary.router, prefix="/custom-vocabulary", tags=["custom-vocabulary"]
)
include_router_with_consistency(
    user_settings.router, prefix="/user-settings", tags=["user-settings"]
)
include_router_with_consistency(
    redaction_settings.user_router, prefix="/user-settings", tags=["redaction-settings"]
)
# RAG chat over the user's transcripts. Router-level capability gate: the whole
# surface 404s when a deployment disables chat (community default: enabled).
include_router_with_consistency(
    chat.router,
    prefix="/chat",
    tags=["chat"],
    capability="chat.rag",
)
include_router_with_consistency(chat.user_router, prefix="/user-settings", tags=["chat-settings"])
include_router_with_consistency(chat.admin_router, prefix="/admin/chat-settings", tags=["admin"])
include_router_with_consistency(
    redaction_settings.admin_router, prefix="/admin/redaction-policy", tags=["admin"]
)
include_router_with_consistency(backup_settings.router, prefix="/admin/backup", tags=["admin"])
include_router_with_consistency(
    media_mirror_settings.router, prefix="/admin/backup/mirror", tags=["admin"]
)
include_router_with_consistency(topics.router, prefix="/files", tags=["topics"])
include_router_with_consistency(
    transcript_segments.router, prefix="/transcripts", tags=["transcript-segments"]
)
include_router_with_consistency(
    embedding_migration.router, prefix="/embeddings/migration", tags=["embedding-migration"]
)
include_router_with_consistency(
    speaker_attribute_migration.router,
    prefix="/speaker-attributes/migration",
    tags=["speaker-attribute-migration"],
)
include_router_with_consistency(
    combined_speaker_migration.router,
    prefix="/speakers/combined-migration",
    tags=["combined-speaker-migration"],
)

# Include WebSocket router without prefix since it handles its own paths
api_router.include_router(websockets.router, tags=["websockets"])
