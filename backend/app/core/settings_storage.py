"""Database, object-storage and Redis connection settings.

Split out of ``app.core.config`` (v0.7.0 L07) as one cohesive concern, following the
``settings_*`` mixin pattern. ``Settings`` inherits these fields, so ``settings.<NAME>`` and
the environment variable names are unchanged. The env source, ``env_ignore_empty`` and
``extra="ignore"`` come from ``Settings``' own ``model_config``; this class is never
instantiated on its own.
"""

import os

from pydantic_settings import BaseSettings

from app.core.env import env_bool

#: Shipped default for :attr:`Settings.CACHE_BUCKET_NAME` — the historical hardcoded
#: value, so an existing deployment that sets nothing keeps the bucket it already has.
#: A module constant so a test can pin the DEFAULT without being at the mercy of the
#: running environment.
DEFAULT_CACHE_BUCKET_NAME = "processed-videos"


class StorageSettings(BaseSettings):
    # Database settings
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT: str = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "opentranscribe")
    POSTGRES_SSLMODE: str = os.getenv("POSTGRES_SSLMODE", "prefer")
    # Plain "" default (not a class-body `os.getenv("DATABASE_URL", <computed>)`
    # expression) resolved in `validate_auth_settings` below. `os.getenv` only
    # substitutes its fallback when the key is ABSENT from the environment, not
    # when it's present-but-empty -- and `.env.example` ships `DATABASE_URL=`
    # (deliberately blank, meaning "assemble it from the POSTGRES_* components").
    # A real `.env` file with that blank line sets `DATABASE_URL=""` in the
    # process environment, so the old class-body form returned "" instead of
    # the computed postgres URL -- crashing `create_engine()` with
    # `Could not parse SQLAlchemy URL from given URL string`. Same bug class as
    # MFA_REQUIRE_REDIS/PKI_REVOCATION_SOFT_FAIL above, just for a str field
    # instead of a bool. `env_ignore_empty=True` cannot fix this: it only
    # affects pydantic-settings' own post-hoc re-sourcing of a field from the
    # environment, not a Python expression that already ran in the class body.
    DATABASE_URL: str = ""

    # Optional database role split (docs-site operations/audit-integrity). POSTGRES_USER stays
    # the owner/migration role; when POSTGRES_APP_USER is set, the backend and workers connect
    # as that unprivileged role at runtime. Resolved in `validate_auth_settings`.
    POSTGRES_APP_USER: str = ""
    POSTGRES_APP_PASSWORD: str = ""
    # False for managed databases where the role is created outside the app.
    POSTGRES_APP_ROLE_PROVISION: bool = True
    # Owner URL used by Alembic and the migration runner; blank = built from POSTGRES_USER.
    MIGRATION_DATABASE_URL: str = ""

    # MinIO / S3 settings
    MINIO_ROOT_USER: str = os.getenv("MINIO_ROOT_USER", "minioadmin")
    MINIO_ROOT_PASSWORD: str = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
    MINIO_HOST: str = os.getenv("MINIO_HOST", "localhost")
    MINIO_PORT: str = os.getenv("MINIO_PORT", "9000")
    MINIO_SECURE: bool = env_bool("MINIO_SECURE", False)
    MEDIA_BUCKET_NAME: str = os.getenv("MEDIA_BUCKET_NAME", "opentranscribe")
    # Second bucket: the REGENERABLE cache. Subtitle-embedded videos and extracted audio
    # under derived/, bulk-export ZIPs under bulk/. It is separate from the media bucket
    # so one lifecycle rule can expire the whole thing without ever touching an original.
    # Configurable for the same reason MEDIA_BUCKET_NAME is (issue #985): S3 bucket names
    # are ONE GLOBAL NAMESPACE, so no deployment can assume a bucket literally called
    # "processed-videos" is reachable by its credentials. While this was a hardcoded
    # literal in VideoProcessingService, such a deployment booted fine and then failed at
    # request time — bulk subtitle export, subtitle-embedded video download and the
    # derived-cache admin endpoints all construct that service, whose
    # _ensure_cache_bucket_exists re-raises — with no knob to point it anywhere else.
    # Plain field default (no class-body os.getenv): pydantic-settings sources the env var
    # itself, and with env_ignore_empty an explicit `CACHE_BUCKET_NAME=` falls back here
    # instead of resolving to "" — the DATABASE_URL/S3_REGION bug class documented below.
    CACHE_BUCKET_NAME: str = DEFAULT_CACHE_BUCKET_NAME

    # ===== Object-storage backend (issue #284 A1.11) =====
    # "minio" (default) keeps the bundled MinIO container exactly as it was: the
    # endpoint is MINIO_HOST:MINIO_PORT, credentials are the static root user/password,
    # and presigned URLs are rewritten onto the /s3 proxy path. "s3" points the same
    # client at real AWS S3 — regional endpoint, SigV4 with the configured region,
    # virtual-host addressing (minio-py switches automatically for AWS hosts), and
    # credentials from the IAM-role chain. Everything below is inert while this is
    # "minio", so a self-hosted install needs none of it.
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "minio")  # minio | s3
    # Explicit S3 endpoint. Leave empty on AWS to derive https://s3.<region>.amazonaws.com;
    # set it for another S3-compatible provider (e.g. https://s3.wasabisys.com).
    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")
    # SigV4 signing region. Wrong region = every request 400s with AuthorizationHeaderMalformed.
    # Falls back to AWS_REGION so a container that already sets the standard AWS var works.
    # Plain "" default; the nested-getenv chain (fall through to AWS_REGION,
    # then "us-east-1") is resolved in `validate_auth_settings` below -- the
    # same self-referential-default bug as DATABASE_URL: an explicit
    # `S3_REGION=` (present-but-empty) made the old `os.getenv("S3_REGION",
    # os.getenv("AWS_REGION", "us-east-1"))` return "" instead of consulting
    # AWS_REGION, since `os.getenv` only substitutes its fallback when the key
    # is absent, not when it's empty.
    S3_REGION: str = ""
    # True (default) resolves credentials through the AWS provider chain — env vars,
    # EKS/IRSA web-identity token, ECS task role, EC2 instance metadata — so no static
    # keys are needed and rotation is automatic. Set false to sign with the static
    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY pair instead.
    S3_USE_IAM_ROLE: bool = env_bool("S3_USE_IAM_ROLE", True)
    # Apply a browser-PUT CORS policy to the media bucket at startup. MinIO already
    # answers any origin, so this only matters on S3, where a missing CORS config makes
    # every direct browser upload fail preflight. OFF by default: overwriting a bucket's
    # CORS configuration is destructive and the bucket may be shared.
    S3_CONFIGURE_BUCKET_CORS: bool = env_bool("S3_CONFIGURE_BUCKET_CORS", False)
    # Origins allowed to PUT directly to the bucket. Empty = reuse CORS_ORIGINS.
    S3_CORS_ALLOWED_ORIGINS: str = os.getenv("S3_CORS_ALLOWED_ORIGINS", "")

    # Presigned URL expiration settings.
    # Video/audio URLs default to 6 hours: a single presigned URL must outlive a long
    # viewing/labeling session of a multi-hour file (a 5-minute URL 403s mid-playback when
    # the player issues a byte-range request after expiry — the <video> element keeps the
    # stale URL even though the frontend refresher updates its variable). Override via env.
    MEDIA_URL_EXPIRE_SECONDS: int = 21600
    # Thumbnail URLs: 15 minutes default - longer since they're static images
    THUMBNAIL_URL_EXPIRE_SECONDS: int = 900
    # Derived-asset cache retention (subtitle-embedded videos + extracted audio in the
    # processed-videos/derived/ prefix). These are a regenerable cache, not storage —
    # they are duplicates of the originals and re-created on demand in seconds. A MinIO
    # lifecycle rule auto-expires them after this many days to bound disk/cloud usage.
    # Baseline default for headless/cloud deployments; the admin UI (DB) overrides it.
    # 0 disables auto-expiry (keep forever). Tune low on laptops, high on big-disk servers.
    DERIVED_CACHE_RETENTION_DAYS: int = 7
    # Public URL for presigned URLs (how browsers access MinIO)
    # Dev: http://localhost:5178 | Prod/nginx: https://yourdomain.com/minio or https://minio.yourdomain.com
    MINIO_PUBLIC_URL: str = os.getenv("MINIO_PUBLIC_URL", "")
    # Backend-agnostic alias for MINIO_PUBLIC_URL (issue #284 A1.12). Presigned URLs were
    # pinned to the internal MinIO host and rewritten onto a hardcoded /s3 path; this is
    # the one knob that decides the browser-facing origin whatever the backend is. Empty
    # falls back to MINIO_PUBLIC_URL, then to /s3 on the minio backend (today's default)
    # and to no rewrite at all on s3, where the signed URL already names a reachable host.
    STORAGE_PUBLIC_URL: str = os.getenv("STORAGE_PUBLIC_URL", "")
    # Hard ceiling on every presigned URL this app mints (issue #284 A1.12). Six hours is
    # the practical cap under an IAM role: a presigned URL dies with the credentials that
    # signed it, and STS session credentials (IMDS/IRSA/ECS) top out at 1–12 h with 6 h a
    # safe common denominator — so a 24 h URL would 403 long before it "expires".
    # Requests above the ceiling are clamped and logged, never rejected.
    PRESIGNED_URL_MAX_SECONDS: int = 21600
    # Object size (MB) at or above which the browser uploads via presigned multipart
    # instead of one presigned PUT (issue #327). Above the backend's single-PUT ceiling
    # multipart is mandatory — 5 GiB on native S3 — and this knob cannot raise the
    # threshold past it. Below the ceiling multipart is what makes an interrupted upload
    # resumable, so the default sits far under it. Raise it to keep more uploads on the
    # single-PUT path; it can never disable multipart for objects that need it.
    MULTIPART_THRESHOLD_MB: int = 512
    # Whether POST /api/files — the fallback that streams a whole file THROUGH the API
    # process instead of browser -> object storage — is accepted (issue #1008). Set false
    # to keep every file byte out of the API: the route 404s before reading the body,
    # /system/capabilities advertises it so the browser never falls back, and
    # /files/prepare reports "direct upload unavailable" (503) instead of handing out
    # a fallback. Presigned single-PUT and multipart uploads are unaffected.
    API_MEDIATED_UPLOAD_ENABLED: bool = True
    # Presigned-URL revocation on quarantine (issue #907). Browser-facing GET presigns
    # (get_file_url, get_presigned_download_url, MinIOService.get_presigned_url) are
    # signed with a dedicated, least-privilege MinIO service-account identity instead
    # of the root credential, whose policy Denies s3:GetObject on any object carrying
    # the quarantine tag — so a takedown revokes an already-minted URL mid-window.
    # MinIO-only (there is no admin API on native S3); fails open to the root client
    # (today's pre-#907 behavior) if the identity cannot be provisioned. See
    # app/services/storage_presign_identity.py and docs/abuse-and-takedown.md.
    STORAGE_PRESIGN_IDENTITY_ENABLED: bool = env_bool("STORAGE_PRESIGN_IDENTITY_ENABLED", True)
    # STORAGE_BACKEND=s3 only: at startup, write a throwaway object to the media and cache
    # buckets and tag/list/delete it, to find a missing takedown permission at boot instead of
    # at the first takedown (app/services/media_revocation_probe.py). Turn off for buckets
    # where a write per boot is unwanted (tight IAM, versioning, Object Lock).
    STORAGE_TAG_PROBE_ENABLED: bool = env_bool("STORAGE_TAG_PROBE_ENABLED", True)
    # Redis settings (for Celery)
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: str = os.getenv("REDIS_PORT", "6379")
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")
    REDIS_USE_TLS: bool = env_bool("REDIS_USE_TLS", False)
    _REDIS_SCHEME: str = "rediss" if env_bool("REDIS_USE_TLS", False) else "redis"
    # Plain "" default resolved in `validate_auth_settings` below -- same bug
    # class as DATABASE_URL: `.env.example` ships `REDIS_URL=` (blank, meaning
    # "assemble it from REDIS_USE_TLS/HOST/PORT/PASSWORD"), and the old
    # class-body `os.getenv("REDIS_URL", <computed>)` returned "" instead of
    # the computed value whenever the key was present-but-empty.
    REDIS_URL: str = ""
