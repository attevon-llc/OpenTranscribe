"""One-off end-to-end test of a cloud ASR provider using its DB-configured key.

Decrypts the api_key from a `user_asr_settings` row, builds the provider, validates the
connection (zero-cost), then runs a real transcription on a short clip and prints the
segment/speaker/word summary. For verifying pyannote.ai + Deepgram against the live APIs.

    docker compose exec -T celery-worker python -m scripts.test_cloud_asr \
        --config-id 849 --audio /tmp/clip30.wav --max-speakers 2
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-id", type=int, required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--max-speakers", type=int, default=2)
    ap.add_argument("--model", default=None, help="override the config's model_name")
    args = ap.parse_args()

    from app.db.session_utils import session_scope
    from app.models.user_asr_settings import UserASRSettings
    from app.services.asr.base import ASRProvider
    from app.services.asr.types import ASRConfig
    from app.utils.encryption import decrypt_api_key

    with session_scope() as db:
        cfg = db.query(UserASRSettings).filter(UserASRSettings.id == args.config_id).first()
        if cfg is None:
            raise SystemExit(f"no user_asr_settings row with id={args.config_id}")
        provider_name = str(cfg.provider)
        model_name = args.model or str(cfg.model_name)
        region = str(cfg.region) if cfg.region else None
        api_key = decrypt_api_key(str(cfg.api_key)) if cfg.api_key else None

    provider: ASRProvider
    if provider_name == "aws":
        # AWS uses two creds (access key + secret) from the boto3 default chain — env vars
        # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (local testing) or an IAM role (on AWS).
        import os

        from app.services.asr.aws_provider import AWSTranscribeProvider

        provider = AWSTranscribeProvider(
            region=str(region or os.getenv("AWS_REGION") or "us-east-1"),
            model_name=model_name,
            access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
    elif not api_key:
        raise SystemExit(f"config id={args.config_id} ({provider_name}) has no API key")
    elif provider_name == "pyannote":
        from app.services.asr.pyannote_provider import PyAnnoteProvider

        provider = PyAnnoteProvider(api_key, model_name)
    elif provider_name == "deepgram":
        from app.services.asr.deepgram_provider import DeepgramProvider

        provider = DeepgramProvider(api_key, model_name)
    elif provider_name == "assemblyai":
        from app.services.asr.assemblyai_provider import AssemblyAIProvider

        provider = AssemblyAIProvider(api_key, model_name)
    elif provider_name == "gladia":
        from app.services.asr.gladia_provider import GladiaProvider

        provider = GladiaProvider(api_key, model_name)
    else:
        raise SystemExit(f"unsupported provider {provider_name}")

    ok, msg, ms = provider.validate_connection()
    print(f"[{provider_name}/{model_name}] validate_connection: ok={ok} msg={msg!r} ({ms:.0f}ms)")
    if not ok:
        raise SystemExit("connection validation failed — not running a paid transcription")

    asr_cfg = ASRConfig(language="en", max_speakers=args.max_speakers, enable_diarization=True)

    def _progress(p: float, m: str) -> None:
        print(f"   …{p * 100:3.0f}% {m}")

    result = provider.transcribe(args.audio, asr_cfg, progress_callback=_progress)

    speakers = sorted({s.speaker for s in result.segments if s.speaker})
    word_total = sum(len(s.words) if s.words else 0 for s in result.segments)
    print(
        f"[{provider_name}] DONE: segments={len(result.segments)} "
        f"has_speakers={result.has_speakers} speakers={speakers} "
        f"words={word_total} lang={result.language}"
    )
    for s in result.segments[:6]:
        wc = len(s.words) if s.words else 0
        print(f"   [{s.start:6.2f}-{s.end:6.2f}] {s.speaker}: {s.text[:72]!r} ({wc}w)")


if __name__ == "__main__":
    main()
