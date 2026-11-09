"""Hardware, AI-model, LLM-provider, Bedrock, ASR-provider and YouTube settings.

Split out of ``app.core.config`` (v0.7.0 L07) as one cohesive concern, following the
``settings_*`` mixin pattern. ``Settings`` inherits these fields, so ``settings.<NAME>`` and
the environment variable names are unchanged. The env source, ``env_ignore_empty`` and
``extra="ignore"`` come from ``Settings``' own ``model_config``; this class is never
instantiated on its own.
"""

import os

from pydantic_settings import BaseSettings

from app.core.env import env_bool


class ProviderSettings(BaseSettings):
    # Hardware Detection Settings (auto-detected by default)
    TORCH_DEVICE: str = os.getenv("TORCH_DEVICE", "auto")  # auto, cuda, mps, cpu
    COMPUTE_TYPE: str = os.getenv("COMPUTE_TYPE", "auto")  # auto, float16, float32, int8
    USE_GPU: str = os.getenv("USE_GPU", "auto")  # auto, true, false
    GPU_DEVICE_ID: int = 0  # Host GPU index (Docker maps to device 0)
    BATCH_SIZE: str = os.getenv("BATCH_SIZE", "auto")  # auto or integer

    # Redaction models: GLiNER name NER is slower/more accurate than spaCy; DEVICE is
    # auto|cpu|cuda[:N]; auto uses a GPU only with this much free VRAM (GB).
    REDACTION_PII_USE_GLINER: bool = False
    REDACTION_DEVICE: str = "auto"
    REDACTION_MIN_FREE_VRAM_GB: float = 1.5

    # AI Models settings
    # large-v3-turbo: 6x faster, ~6GB VRAM, excellent English, good multilingual
    # large-v3: Best accuracy, ~10GB VRAM, required for translation feature
    # large-v2: Legacy model, ~10GB VRAM, good balance
    # Note: large-v3-turbo cannot translate - use large-v3 if translation is needed
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "large-v3-turbo")
    HUGGINGFACE_TOKEN: str | None = os.getenv("HUGGINGFACE_TOKEN", None)

    # Speaker diarization settings
    MIN_SPEAKERS: int = 1
    MAX_SPEAKERS: int = 20
    # NUM_SPEAKERS forces exact speaker count (overrides min/max if set). An
    # unset/empty env var means "genuinely unset" and falls back to the MIN/MAX
    # range via `env_ignore_empty` on `model_config` above; a malformed non-empty
    # value (e.g. a stray inline `#` comment on the same .env line, which docker
    # compose's env_file loading does not strip) raises `ValidationError` at
    # startup instead of silently degrading — deliberate, see model_config's
    # docstring above.
    NUM_SPEAKERS: int | None = None

    # Diarization embedding batch size is pinned at 16 in
    # backend/app/transcription/diarizer.py. See
    # docs/diarization-vram-profile/README.md for the Phase A measurements
    # that justify the fixed ceiling (identical throughput from bs=16 to
    # bs=128; 3-7 GB VRAM savings at bs=16). No env var is offered for
    # production because the measured-optimal value is a constant.

    # LLM Configuration - Users configure through web UI, stored in database
    # These are system fallbacks for quick access when no user settings exist
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "")

    # Quick access defaults for common providers
    VLLM_BASE_URL: str = os.getenv("VLLM_BASE_URL", "http://localhost:8012/v1")
    VLLM_MODEL_NAME: str = os.getenv("VLLM_MODEL_NAME", "gpt-oss")
    VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "")

    OPENAI_MODEL_NAME: str = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL_NAME: str = os.getenv("OLLAMA_MODEL_NAME", "llama2:7b-chat")

    # Haiku 4.5 is the documented replacement for the deprecated claude-3-haiku-20240307.
    # Deliberately still the Haiku tier: this default drives the batch enrichment tasks
    # (summarization, topic extraction, speaker ID) where per-transcript cost matters more
    # than frontier reasoning. Operators wanting a stronger model for chat set
    # ANTHROPIC_MODEL_NAME, or pin one per-conversation in the chat UI.
    ANTHROPIC_MODEL_NAME: str = os.getenv("ANTHROPIC_MODEL_NAME", "claude-haiku-4-5")
    ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # NOTE the slug convention differs from Anthropic's first-party API: OpenRouter
    # uses a dot ("claude-haiku-4.5"), the first-party ID uses dashes
    # ("claude-haiku-4-5"). Mixing them yields a 404 from whichever side is wrong.
    OPENROUTER_MODEL_NAME: str = os.getenv("OPENROUTER_MODEL_NAME", "anthropic/claude-haiku-4.5")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    # OpenRouter app attribution (sent only when OpenRouter is the configured provider).
    # Defaults to the public project repo so no install identifies itself by accident;
    # set to an empty string to omit the header entirely.
    OPENROUTER_HTTP_REFERER: str = os.getenv(
        "OPENROUTER_HTTP_REFERER", "https://github.com/attevon-llc/OpenTranscribe"
    )

    # ===== Amazon Bedrock =====
    # AWS-native LLM access via the Converse API. There is deliberately NO API-key
    # setting: boto3 resolves credentials through the standard chain (instance role,
    # task role, profile, environment), so a deployment on EC2/ECS/EKS needs no secret
    # provisioned at all — which is most of the operational appeal over a raw API key.
    # Region falls back to the AWS SDK's own variables so an already-configured host
    # needs nothing extra.
    # Plain "" default; the nested-getenv chain (AWS_REGION, then
    # AWS_DEFAULT_REGION, then "") is resolved in `validate_auth_settings`
    # below -- same self-referential-default bug as DATABASE_URL/S3_REGION.
    # `.env.example` ships `BEDROCK_REGION=` blank.
    BEDROCK_REGION: str = ""
    # Bare foundation-model ID; a geography prefix is applied at call time to select the
    # cross-region inference profile (see llm_bedrock.resolve_model_id). Set a fully
    # prefixed ID or a profile ARN here to bypass that and pin an exact profile.
    BEDROCK_MODEL_NAME: str = os.getenv(
        "BEDROCK_MODEL_NAME", "anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    # botocore retry policy for every Bedrock client, streaming included (issue #1049).
    # `adaptive` adds client-side rate limiting on top of `standard`'s jittered
    # exponential backoff; 8 attempts rides out a short capacity blip
    # (ServiceUnavailable/Throttling) that the botocore default surfaced as an error.
    BEDROCK_RETRY_MODE: str = "adaptive"
    BEDROCK_MAX_ATTEMPTS: int = 8

    # ===== ASR (Speech Recognition) Provider =====
    ASR_PROVIDER: str = os.getenv("ASR_PROVIDER", "local")
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_MODEL: str = os.getenv("DEEPGRAM_MODEL", "nova-3")
    ASSEMBLYAI_API_KEY: str = os.getenv("ASSEMBLYAI_API_KEY", "")
    ASSEMBLYAI_MODEL: str = os.getenv("ASSEMBLYAI_MODEL", "universal")
    OPENAI_ASR_MODEL: str = os.getenv("OPENAI_ASR_MODEL", "gpt-4o-transcribe")
    # Google Cloud Speech — credentials file path (service account JSON)
    GOOGLE_CLOUD_CREDENTIALS: str = os.getenv("GOOGLE_CLOUD_CREDENTIALS", "")
    GOOGLE_ASR_MODEL: str = os.getenv("GOOGLE_ASR_MODEL", "chirp-3")
    AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
    AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "eastus")
    AZURE_ASR_MODEL: str = os.getenv("AZURE_ASR_MODEL", "whisper")
    # AWS Transcribe — credentials (can also use IAM role / instance profile)
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    AWS_ASR_MODEL: str = os.getenv("AWS_ASR_MODEL", "standard")
    AWS_TRANSCRIBE_BUCKET: str = os.getenv("AWS_TRANSCRIBE_BUCKET", "")
    SPEECHMATICS_API_KEY: str = os.getenv("SPEECHMATICS_API_KEY", "")
    SPEECHMATICS_MODEL: str = os.getenv("SPEECHMATICS_MODEL", "standard")
    GLADIA_API_KEY: str = os.getenv("GLADIA_API_KEY", "")
    GLADIA_MODEL: str = os.getenv("GLADIA_MODEL", "standard")
    PYANNOTE_API_KEY: str = os.getenv("PYANNOTE_API_KEY", "")
    PYANNOTE_MODEL: str = os.getenv("PYANNOTE_MODEL", "parakeet")

    # ===== OpenSearch Toggle =====
    OPENSEARCH_ENABLED: bool = env_bool("OPENSEARCH_ENABLED", True)

    # ===== YouTube Anti-Bot Detection Configuration =====
    # Cookie-Based Authentication (allows yt-dlp to use browser cookies)
    YOUTUBE_COOKIE_BROWSER: str | None = os.getenv(
        "YOUTUBE_COOKIE_BROWSER", None
    )  # firefox, chrome, chromium, edge, safari, opera
    YOUTUBE_COOKIE_FILE: str | None = os.getenv(
        "YOUTUBE_COOKIE_FILE", None
    )  # Path to cookies.txt file

    # Playlist Staggering (progressive delays when dispatching videos)
    YOUTUBE_PLAYLIST_STAGGER_ENABLED: bool = env_bool("YOUTUBE_PLAYLIST_STAGGER_ENABLED", True)
    YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS: int = 5
    YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS: int = 30
    YOUTUBE_PLAYLIST_STAGGER_INCREMENT: int = 5

    # Pre-Download Jitter (random delay before each download starts)
    YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED: bool = env_bool(
        "YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", True
    )
    YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS: int = 2
    YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS: int = 15

    # The per-user quotas, the recovery batch size and the automatic-retry switch are admin
    # settings (``services/youtube_policy``), not environment variables.

    # Celery task-level rate limit for YouTube downloads.
    # Format: "N/h" (per hour), "N/m" (per minute), "N/s" (per second).
    # This is enforced by the download worker regardless of how many tasks
    # are queued.  Set to "0" or empty to disable.
    YOUTUBE_DOWNLOAD_RATE_LIMIT: str = os.getenv("YOUTUBE_DOWNLOAD_RATE_LIMIT", "30/h")

    # Performance optimization properties
    @property
    def effective_use_gpu(self) -> bool:
        """Determine if GPU should be used based on hardware detection."""
        if self.USE_GPU.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.device in ["cuda", "mps"]
            except ImportError:
                return False
        return self.USE_GPU.lower() == "true"

    @property
    def effective_torch_device(self) -> str:
        """Get the effective torch device."""
        if self.TORCH_DEVICE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.device
            except ImportError:
                return "cpu"
        return self.TORCH_DEVICE.lower()

    @property
    def effective_compute_type(self) -> str:
        """Get the effective compute type."""
        if self.COMPUTE_TYPE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.compute_type
            except ImportError:
                return "int8"
        return self.COMPUTE_TYPE.lower()

    @property
    def effective_batch_size(self) -> int:
        """Get the effective batch size."""
        if self.BATCH_SIZE.lower() == "auto":
            try:
                from app.utils.hardware_detection import detect_hardware

                config = detect_hardware()
                return config.batch_size
            except ImportError:
                return 1
        return int(self.BATCH_SIZE)
