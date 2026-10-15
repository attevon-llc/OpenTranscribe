"""Amazon Bedrock connection details: the ONE place a Bedrock client is configured.

A Bedrock LLM configuration may carry its own AWS region and credentials (access key id,
secret access key, optional session token). They are stored as a single encrypted blob in the
configuration's ``api_key`` column (``utils/encryption``), are write-only, and never appear in a
response, a log line or an error message. A configuration that leaves them blank uses the
server's own credential chain (IAM role, shared profile, environment) and the deployment region.

Everything that talks to Bedrock goes through here:

* :func:`resolve_region` — the configuration's region, else ``BEDROCK_REGION`` /
  ``AWS_REGION`` / ``AWS_DEFAULT_REGION``;
* :func:`client_kwargs` — the ONLY builder of boto3 client keyword arguments (runtime calls,
  the connection test and the model listing all use it);
* :func:`describe_failure` — maps a boto3 exception to a fixed sentence, never raw text.

No function here touches the database, so a Celery task can resolve a connection once, close
its session, and call Bedrock with none open.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.core import constants
from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BedrockConnection:
    """A Bedrock configuration's own region and credentials. Secrets never print."""

    region: str | None = None
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)

    @property
    def has_credentials(self) -> bool:
        """True when this configuration carries its own key pair."""
        return bool(self.access_key_id and self.secret_access_key)

    def secrets(self) -> tuple[str, ...]:
        """Every secret-bearing value, for scrubbing text."""
        return tuple(
            v for v in (self.access_key_id, self.secret_access_key, self.session_token) if v
        )


def pack_stored(connection: BedrockConnection) -> str | None:
    """JSON for :func:`utils.encryption.encrypt_api_key`, or ``None`` when there is nothing."""
    data = {
        "region": connection.region,
        "access_key_id": connection.access_key_id,
        "secret_access_key": connection.secret_access_key,
        "session_token": connection.session_token,
    }
    data = {k: v for k, v in data.items() if v}
    return json.dumps(data, separators=(",", ":")) if data else None


def unpack_stored(plaintext: str | None) -> BedrockConnection:
    """Rebuild a connection from a decrypted blob; a malformed blob reads as empty."""
    if not plaintext:
        return BedrockConnection()
    try:
        raw = json.loads(plaintext)
    except ValueError:
        return BedrockConnection()
    if not isinstance(raw, dict):
        return BedrockConnection()

    def _s(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    return BedrockConnection(
        region=_s("region"),
        access_key_id=_s("access_key_id"),
        secret_access_key=_s("secret_access_key"),
        session_token=_s("session_token"),
    )


def merge_stored(
    existing: BedrockConnection,
    *,
    region: str | None,
    region_given: bool,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None,
    clear_credentials: bool,
) -> BedrockConnection:
    """Apply an edit to a stored connection.

    A blank credential field keeps what is stored ("leave blank to keep it"); a new key pair
    replaces the stored one entirely (so a stale session token never rides along);
    ``clear_credentials`` drops the pair so the configuration uses the server's chain. The
    region is replaced only when the request names it.
    """
    new_region = (region or None) if region_given else existing.region
    key_id, secret, token = (
        existing.access_key_id,
        existing.secret_access_key,
        existing.session_token,
    )
    if clear_credentials:
        key_id = secret = token = None
    if access_key_id and secret_access_key:
        key_id, secret, token = access_key_id, secret_access_key, session_token or None
    return BedrockConnection(
        region=new_region, access_key_id=key_id, secret_access_key=secret, session_token=token
    )


def connection_from_encrypted(encrypted: str | None) -> BedrockConnection:
    """Decrypt a stored ``api_key`` column value into a connection."""
    if not encrypted:
        return BedrockConnection()
    from app.utils.encryption import decrypt_api_key

    return unpack_stored(decrypt_api_key(str(encrypted)))


def public_view(encrypted: str | None) -> tuple[str | None, bool]:
    """``(region, has_credentials)`` for a response. The only fields a client may see."""
    connection = connection_from_encrypted(encrypted)
    return connection.region, connection.has_credentials


def env_region() -> str:
    """The deployment region. ``Settings`` has already folded AWS_REGION and
    AWS_DEFAULT_REGION into ``BEDROCK_REGION`` when the latter is blank (core/config.py)."""
    return str(settings.BEDROCK_REGION or "").strip()


def resolve_region(connection: BedrockConnection | None = None) -> str:
    """The region a call goes to: the configuration's, else the deployment's, else ``""``."""
    if connection is not None and connection.region:
        return connection.region
    return env_region()


def resolve_default_model() -> str:
    """Model id for the environment-configured (no per-user configuration) fallback."""
    return str(settings.BEDROCK_MODEL_NAME or "").strip() or constants.DEFAULT_BEDROCK_MODEL_ID


def client_kwargs(
    region: str, connection: BedrockConnection | None, *, config: Any | None = None
) -> dict[str, Any]:
    """The boto3 ``client(...)`` keyword arguments for ``region`` and ``connection``.

    The configuration's own key pair is passed explicitly; without one boto3's standard chain
    is left to resolve credentials. This is the only place credentials are handed to boto3.
    """
    kwargs: dict[str, Any] = {"region_name": region}
    if config is not None:
        kwargs["config"] = config
    if connection is not None and connection.has_credentials:
        kwargs["aws_access_key_id"] = connection.access_key_id
        kwargs["aws_secret_access_key"] = connection.secret_access_key
        if connection.session_token:
            kwargs["aws_session_token"] = connection.session_token
    return kwargs


def scrub(text: str, connection: BedrockConnection | None) -> str:
    """Remove every secret-bearing value of ``connection`` from ``text``."""
    if connection is None:
        return text
    for secret in connection.secrets():
        text = text.replace(secret, "***")
    return text


#: Fixed sentences for every Bedrock failure a user can act on. Raw exception text is never
#: returned (error_categorization_service's no-raw-echo contract).
FAILURE_MESSAGES = {
    "no_region": "No AWS region is set. Choose a region for this configuration.",
    "no_credentials": (
        "No AWS credentials were found. Enter an access key, or run the server with an AWS role."
    ),
    "bad_credentials": "AWS rejected these credentials. Check the access key, secret and token.",
    "access_denied": (
        "AWS denied access. The credentials need permission to invoke Amazon Bedrock models."
    ),
    "model_not_enabled": (
        "This model is not enabled for your AWS account. Request access to it in the Amazon "
        "Bedrock console."
    ),
    "needs_inference_profile": (
        "This model cannot be called by its base id. Use an inference profile id such as "
        "global.anthropic.claude-haiku-4-5-20251001-v1:0."
    ),
    "region_unavailable": "Amazon Bedrock or this model is not available in the chosen region.",
    "unreachable": "The Amazon Bedrock endpoint could not be reached from this server.",
    "throttled": "AWS is throttling requests. Try again shortly.",
    "failed": "Connection failed. Check the region, model id and credentials.",
}


def classify_failure(exc: BaseException) -> str:
    """Map a boto3 / botocore exception to a key of :data:`FAILURE_MESSAGES`."""
    from botocore.exceptions import ClientError
    from botocore.exceptions import ConnectTimeoutError
    from botocore.exceptions import EndpointConnectionError
    from botocore.exceptions import NoCredentialsError
    from botocore.exceptions import NoRegionError
    from botocore.exceptions import PartialCredentialsError

    if isinstance(exc, NoRegionError):
        return "no_region"
    if isinstance(exc, NoCredentialsError | PartialCredentialsError):
        return "no_credentials"
    if isinstance(exc, EndpointConnectionError | ConnectTimeoutError):
        return "unreachable"
    if isinstance(exc, ValueError):
        # botocore rejects a malformed region name before any request.
        return "region_unavailable"
    if isinstance(exc, ClientError):
        error = exc.response.get("Error") or {}
        code = str(error.get("Code", ""))
        message = str(error.get("Message", "")).lower()
        if code in {
            "UnrecognizedClientException",
            "InvalidSignatureException",
            "ExpiredTokenException",
            "InvalidClientTokenId",
            "SignatureDoesNotMatch",
        }:
            return "bad_credentials"
        if code == "AccessDeniedException":
            return "model_not_enabled" if "access to the model" in message else "access_denied"
        if code == "ResourceNotFoundException":
            return "model_not_enabled" if "model" in message else "region_unavailable"
        if code == "ValidationException" and (
            "on-demand throughput" in message or "inference profile" in message
        ):
            return "needs_inference_profile"
        if code in {"ThrottlingException", "TooManyRequestsException"}:
            return "throttled"
    return "failed"


def describe_failure(exc: BaseException) -> str:
    """The fixed sentence for ``exc``."""
    return FAILURE_MESSAGES[classify_failure(exc)]


#: botocore credential-provider ``method`` values mapped to a kind the UI can name. Anything
#: unlisted is reported as "other": the kind is informational, never a secret.
_CREDENTIAL_KINDS = {
    "env": "environment",
    "shared-credentials-file": "shared_profile",
    "config-file": "shared_profile",
    "custom-process": "shared_profile",
    "sso": "shared_profile",
    "login": "shared_profile",
    "boto-config": "shared_profile",
    "assume-role": "assumed_role",
    "assume-role-with-web-identity": "assumed_role",
    "iam-role": "instance_role",
    "container-role": "container_role",
}


def server_credential_status() -> dict[str, object]:
    """Whether the SERVER's own boto3 chain finds credentials, and the KIND of source.

    Makes no AWS call. Only ``Credentials.method`` is read: the key, secret and token are
    never touched, returned or logged.
    """
    try:
        from botocore.session import get_session

        creds = get_session().get_credentials()
    except Exception as exc:  # noqa: BLE001 -- a broken profile file must read as "not found", not a 500
        logger.warning("AWS credential lookup failed: %s", type(exc).__name__)
        return {"found": False, "source": None}
    if creds is None:
        return {"found": False, "source": None}
    method = str(getattr(creds, "method", "") or "")
    return {"found": True, "source": _CREDENTIAL_KINDS.get(method, "other")}
