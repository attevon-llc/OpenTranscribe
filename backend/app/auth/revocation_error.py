"""The failure a token revocation reports when it cannot be recorded durably.

Raised by ``TokenService.revoke_token`` and caught by refresh-token rotation and the
logout endpoints, so it lives below both of them.
"""


class RevocationError(Exception):
    """A token revocation could not be recorded durably.

    Carries no cause text: callers answer with a fixed code and the original
    exception is chained for the server log only.
    """
