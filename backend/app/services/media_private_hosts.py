"""Write side of the super-admin allowlist of private-network media hosts.

The read side (and the rule it enforces) is ``utils/media_private_hosts``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.schemas.media_source import normalise_source_address
from app.services.system_settings_service import set_setting
from app.utils.media_private_hosts import SETTING_KEY

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

MAX_HOSTS = 200


def set_private_hosts(db: Session, addresses: list[str]) -> list[str]:
    """Validate, de-duplicate and store the allowlist; returns what was stored.

    Raises:
        ValueError: if an address is malformed or the list is too long.
    """
    normalised: list[str] = []
    for address in addresses:
        if not address.strip():
            continue
        value = normalise_source_address(address)
        if value not in normalised:
            normalised.append(value)
    if len(normalised) > MAX_HOSTS:
        raise ValueError(f"At most {MAX_HOSTS} private hosts can be listed")
    set_setting(
        db,
        SETTING_KEY,
        json.dumps(normalised),
        "Private-network media hosts a super administrator has allowed (scheme://host[:port])",
    )
    return normalised
