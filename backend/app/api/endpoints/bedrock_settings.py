"""Read-only status of the SERVER's own AWS credential chain, for the Bedrock form.

A Bedrock LLM configuration may carry its own credentials (``llm_settings.py``) or leave
them blank to use the server's. This endpoint tells an administrator whether the second
option would work: whether boto3's standard chain finds credentials, and the KIND of source
(environment variables, shared profile, container or instance role). It makes no AWS call and
never reads, returns or logs a key, secret or token.
"""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.schemas.llm_settings import BedrockServerCredentialStatus
from app.services import bedrock_settings

router = APIRouter()


@router.get("/server-credentials", response_model=BedrockServerCredentialStatus)
def get_bedrock_server_credentials(
    current_user: models.User = Depends(get_current_admin_user),
) -> Any:
    """Whether the server's AWS credential chain resolves, plus its deployment region."""
    return BedrockServerCredentialStatus(
        region=bedrock_settings.env_region() or None,
        **bedrock_settings.server_credential_status(),  # type: ignore[arg-type]
    )
