"""Connection parameters shared by every OpenSearch client in the app.

Issue #284 A1.13. The same ``OpenSearch(hosts=..., http_auth=(user, password), ...)``
literal was written out at five call sites (the search plane, the audit logger's
writer and reader, and the admin audit export). Basic auth is what the bundled
container wants; an Amazon OpenSearch Service domain whose access policy is IAM-based
accepts **only** SigV4-signed requests, so making the managed case work meant changing
all five. They now share this one builder.

``OPENSEARCH_AUTH=basic`` is the default and reproduces the previous kwargs exactly.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_sigv4() -> bool:
    """Whether OpenSearch requests must be SigV4-signed."""
    return settings.OPENSEARCH_AUTH.strip().lower() == "sigv4"


def signing_region() -> str:
    """AWS region used to sign OpenSearch requests."""
    return (settings.OPENSEARCH_AWS_REGION or settings.AWS_REGION).strip() or "us-east-1"


def _sigv4_auth() -> Any:
    """Build the SigV4 request signer from the ambient AWS credential chain.

    Raises:
        RuntimeError: If botocore resolves no credentials. Failing here is
            deliberate — falling back to basic auth would send the configured
            OpenSearch password to an AWS endpoint that never wanted it.
    """
    import boto3
    from opensearchpy import AWSV4SignerAuth

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError(
            "OPENSEARCH_AUTH=sigv4 but no AWS credentials could be resolved "
            "(env vars, IRSA/web identity, ECS task role, or EC2 instance metadata)"
        )
    return AWSV4SignerAuth(credentials, signing_region(), settings.OPENSEARCH_AWS_SERVICE)


def opensearch_connection_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build the keyword arguments for an ``opensearchpy.OpenSearch`` client.

    Args:
        **overrides: Extra client kwargs (``timeout``, ``pool_maxsize``, …) merged
            on top of the resolved connection settings.

    Returns:
        A kwargs dict ready to splat into ``OpenSearch(**kwargs)``.

    Note:
        SigV4 forces ``connection_class=RequestsHttpConnection`` (overriding any
        caller value). ``AWSV4SignerAuth`` is a ``requests.auth.AuthBase``; under
        opensearch-py's default ``Urllib3HttpConnection`` it is silently ignored and
        every request goes out unsigned, which surfaces as a blanket 403 from the
        domain. Basic auth leaves the transport to the caller so the existing clients
        keep the connection class they already used.
    """
    kwargs: dict[str, Any] = {
        "hosts": [{"host": settings.OPENSEARCH_HOST, "port": int(settings.OPENSEARCH_PORT)}],
        "use_ssl": settings.OPENSEARCH_USE_TLS,
        "verify_certs": settings.OPENSEARCH_VERIFY_CERTS,
        "ssl_show_warn": False,
        "http_auth": (settings.OPENSEARCH_USER, settings.OPENSEARCH_PASSWORD),
    }
    kwargs.update(overrides)

    if is_sigv4():
        from opensearchpy import RequestsHttpConnection

        kwargs["http_auth"] = _sigv4_auth()
        kwargs["connection_class"] = RequestsHttpConnection
        # A managed domain is TLS-only, and signing over plaintext would put a
        # replayable Authorization header on the wire.
        if not settings.OPENSEARCH_USE_TLS:
            logger.warning("OPENSEARCH_AUTH=sigv4 forces TLS on; ignoring OPENSEARCH_USE_TLS=false")
        kwargs["use_ssl"] = True
        kwargs["verify_certs"] = True

    return kwargs
