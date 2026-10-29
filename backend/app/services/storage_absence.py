"""Telling "the object is absent" apart from "storage did not answer".

A broad ``except`` that maps every failure to "absent" turns an outage into a
legitimate-looking empty result. Callers that need the distinction use
``is_absent_error`` and raise (or retry) on everything else.
"""

from minio.error import S3Error

#: S3 error codes that mean "the object is not there" — the only answers that are not an outage.
_ABSENT_S3_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "ResourceNotFound"})


def is_absent_error(exc: BaseException) -> bool:
    """True when ``exc`` (or anything in its cause chain) is a definite "object absent" answer.

    ``MinIOService`` wraps every client failure in a plain ``Exception`` and keeps the real
    ``S3Error`` only in ``__cause__``, so the chain is walked. Connection errors, timeouts,
    5xx and AccessDenied are NOT absence: the storage did not answer the question.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, S3Error) and cur.code in _ABSENT_S3_CODES:
            return True
        cur = cur.__cause__
    return False
